"""All SQL lives here. Routers and AI tools call these functions and nothing else.

Every statement is parameterised. There is no string interpolation of user input
anywhere in this module -- symbol names, timestamps and limits are all bound
parameters, so a symbol of `'; DROP TABLE ticks; --` is a value that matches no
row rather than a statement fragment.
"""
from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# --------------------------------------------------------------------------
# Cursor helpers
# --------------------------------------------------------------------------
def encode_cursor(ts: datetime, trade_id: int) -> str:
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{trade_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, _, tid = raw.rpartition("|")
        return datetime.fromisoformat(ts_str), int(tid)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("malformed cursor") from exc


# --------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------
async def list_symbols(db: AsyncSession, active_only: bool = True) -> list[dict]:
    sql = text(
        """
        SELECT symbol, base_asset, quote_asset, is_active
        FROM symbols
        WHERE (:active_only = FALSE OR is_active)
        ORDER BY symbol
        """
    )
    rows = await db.execute(sql, {"active_only": active_only})
    return [dict(r) for r in rows.mappings()]


async def symbol_exists(db: AsyncSession, symbol: str) -> bool:
    row = await db.execute(
        text("SELECT 1 FROM symbols WHERE symbol = :s AND is_active"), {"s": symbol}
    )
    return row.first() is not None


# --------------------------------------------------------------------------
# Ticks
# --------------------------------------------------------------------------
async def latest_price(db: AsyncSession, symbol: str) -> dict | None:
    # Index-only friendly: idx_ticks_symbol_ts is (symbol, ts DESC), so this is a
    # single index seek to the leading edge, then LIMIT 1. No sort, no scan.
    sql = text(
        """
        SELECT symbol, price, qty, ts
        FROM ticks
        WHERE symbol = :symbol
        ORDER BY ts DESC
        LIMIT 1
        """
    )
    row = await db.execute(sql, {"symbol": symbol})
    m = row.mappings().first()
    return dict(m) if m else None


async def latest_prices(db: AsyncSession, symbols: list[str]) -> list[dict]:
    """One lateral join instead of N round trips.

    DISTINCT ON would also work, but it forces a scan across every symbol's rows.
    LATERAL runs the cheap "top 1 for this symbol" seek once per symbol.
    """
    sql = text(
        """
        SELECT s.symbol, t.price, t.qty, t.ts
        FROM unnest(CAST(:symbols AS text[])) AS s(symbol)
        LEFT JOIN LATERAL (
            SELECT price, qty, ts
            FROM ticks
            WHERE ticks.symbol = s.symbol
            ORDER BY ts DESC
            LIMIT 1
        ) t ON TRUE
        WHERE t.ts IS NOT NULL
        ORDER BY s.symbol
        """
    )
    rows = await db.execute(sql, {"symbols": symbols})
    return [dict(r) for r in rows.mappings()]


async def get_ticks(
    db: AsyncSession,
    symbol: str,
    start: datetime,
    end: datetime,
    limit: int,
    cursor: str | None = None,
) -> tuple[list[dict], str | None, bool]:
    """Keyset-paginated tick history, newest first.

    The cursor is the (ts, trade_id) of the last row the client saw. Comparing
    the row tuple against it is a single index seek regardless of how deep the
    client has paged -- unlike OFFSET, whose cost grows linearly with depth.
    """
    params: dict = {"symbol": symbol, "start": start, "end": end, "limit": limit + 1}
    keyset = ""
    if cursor:
        cur_ts, cur_id = decode_cursor(cursor)
        params["cur_ts"] = cur_ts
        params["cur_id"] = cur_id
        keyset = "AND (ts, trade_id) < (:cur_ts, :cur_id)"

    sql = text(
        f"""
        SELECT symbol, trade_id, price, qty, quote_qty, is_buyer_maker, ts
        FROM ticks
        WHERE symbol = :symbol
          AND ts >= :start
          AND ts <  :end
          {keyset}
        ORDER BY ts DESC, trade_id DESC
        LIMIT :limit
        """
    )
    rows = [dict(r) for r in (await db.execute(sql, params)).mappings()]

    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1]["ts"], rows[-1]["trade_id"]) if rows and has_more else None
    return rows, next_cursor, has_more


# --------------------------------------------------------------------------
# OHLCV
# --------------------------------------------------------------------------
async def get_ohlcv(
    db: AsyncSession, symbol: str, start: datetime, end: datetime, limit: int
) -> list[dict]:
    sql = text(
        """
        SELECT symbol, bucket, open, high, low, close, volume, quote_volume, trade_count
        FROM ohlcv_1m
        WHERE symbol = :symbol AND bucket >= :start AND bucket < :end
        ORDER BY bucket ASC
        LIMIT :limit
        """
    )
    rows = await db.execute(sql, {"symbol": symbol, "start": start, "end": end, "limit": limit})
    return [dict(r) for r in rows.mappings()]


async def ohlcv_summary(
    db: AsyncSession, symbol: str, start: datetime, end: datetime
) -> dict | None:
    """Aggregate a window into one row. Used by the AI layer so the model never
    has to receive (or pay for) thousands of candles just to answer "what was the
    high yesterday"."""
    sql = text(
        """
        WITH w AS (
            SELECT * FROM ohlcv_1m
            WHERE symbol = :symbol AND bucket >= :start AND bucket < :end
        )
        SELECT
            (SELECT open  FROM w ORDER BY bucket ASC  LIMIT 1) AS open,
            (SELECT close FROM w ORDER BY bucket DESC LIMIT 1) AS close,
            MAX(high)            AS high,
            MIN(low)             AS low,
            SUM(volume)          AS volume,
            SUM(quote_volume)    AS quote_volume,
            SUM(trade_count)     AS trade_count,
            COUNT(*)             AS candle_count,
            MIN(bucket)          AS first_bucket,
            MAX(bucket)          AS last_bucket
        FROM w
        """
    )
    row = await db.execute(sql, {"symbol": symbol, "start": start, "end": end})
    m = row.mappings().first()
    if not m or m["candle_count"] == 0:
        return None
    return dict(m)


async def largest_moves(
    db: AsyncSession, symbol: str, start: datetime, end: datetime, top_n: int = 5
) -> list[dict]:
    """The N largest single-minute moves in a window, by absolute % change."""
    sql = text(
        """
        SELECT
            bucket,
            open,
            close,
            high,
            low,
            volume,
            CASE WHEN open > 0
                 THEN ROUND(((close - open) / open) * 100, 4)
                 ELSE 0 END AS pct_change,
            CASE WHEN low > 0
                 THEN ROUND(((high - low) / low) * 100, 4)
                 ELSE 0 END AS pct_range
        FROM ohlcv_1m
        WHERE symbol = :symbol AND bucket >= :start AND bucket < :end AND open > 0
        ORDER BY ABS((close - open) / open) DESC
        LIMIT :top_n
        """
    )
    rows = await db.execute(
        sql, {"symbol": symbol, "start": start, "end": end, "top_n": top_n}
    )
    return [dict(r) for r in rows.mappings()]


async def compare_symbols(
    db: AsyncSession, symbols: list[str], start: datetime, end: datetime
) -> list[dict]:
    """Rank symbols over one window: return, realised volatility, volume.

    Volatility is the population standard deviation of per-minute returns,
    annualised to a daily figure (sqrt(1440) minutes per day) so the number is
    comparable across window lengths.
    """
    sql = text(
        """
        WITH ranked AS (
            SELECT
                symbol, bucket, open, close, high, low, volume, quote_volume,
                LAG(close) OVER (PARTITION BY symbol ORDER BY bucket) AS prev_close
            FROM ohlcv_1m
            WHERE symbol = ANY(CAST(:symbols AS text[]))
              AND bucket >= :start AND bucket < :end
        ),
        rets AS (
            SELECT symbol, bucket, close, open, high, low, volume, quote_volume,
                   CASE WHEN prev_close > 0 THEN (close - prev_close) / prev_close END AS ret
            FROM ranked
        )
        SELECT
            symbol,
            COUNT(*)                                        AS candle_count,
            (ARRAY_AGG(open  ORDER BY bucket ASC))[1]       AS first_open,
            (ARRAY_AGG(close ORDER BY bucket DESC))[1]      AS last_close,
            MAX(high)                                       AS high,
            MIN(low)                                        AS low,
            SUM(volume)                                     AS volume,
            SUM(quote_volume)                               AS quote_volume,
            -- SQRT(1440) is double precision, and numeric * double is double,
            -- which has no two-argument ROUND. Keeping the whole expression in
            -- NUMERIC preserves exactness and keeps ROUND(value, scale) valid.
            ROUND(COALESCE(STDDEV_POP(ret), 0) * SQRT(1440::NUMERIC) * 100, 4) AS daily_vol_pct
        FROM rets
        GROUP BY symbol
        ORDER BY symbol
        """
    )
    rows = await db.execute(sql, {"symbols": symbols, "start": start, "end": end})
    out = []
    for r in rows.mappings():
        d = dict(r)
        if d["first_open"] and d["first_open"] > 0:
            d["pct_change"] = round(
                float((d["last_close"] - d["first_open"]) / d["first_open"]) * 100, 4
            )
        else:
            d["pct_change"] = 0.0
        out.append(d)
    return out


# --------------------------------------------------------------------------
# Operational
# --------------------------------------------------------------------------
async def ingest_stats(db: AsyncSession) -> dict:
    sql = text(
        """
        SELECT
            (SELECT COUNT(*) FROM ohlcv_1m)                             AS ohlcv_rows,
            (SELECT MAX(ts) FROM ticks)                                 AS last_tick_ts,
            (SELECT MAX(bucket) FROM ohlcv_1m)                          AS last_bucket,
            (SELECT COUNT(*) FROM pg_class c
               JOIN pg_inherits i ON i.inhrelid = c.oid
               JOIN pg_class p ON p.oid = i.inhparent
              WHERE p.relname = 'ticks')                                AS tick_partitions,
            -- pg_total_relation_size on a partitioned parent counts only the
            -- parent's own (empty) storage. The real size is the sum of the
            -- partitions, so sum across the inheritance children.
            (SELECT pg_size_pretty(COALESCE(SUM(pg_total_relation_size(c.oid)), 0))
               FROM pg_class c
               JOIN pg_inherits i ON i.inhrelid = c.oid
               JOIN pg_class p ON p.oid = i.inhparent
              WHERE p.relname = 'ticks')                                AS ticks_size
        """
    )
    row = await db.execute(sql)
    return dict(row.mappings().one())


async def approx_tick_count(db: AsyncSession) -> int:
    """Planner estimate, not COUNT(*).

    An exact count on a partitioned table with millions of rows is a full scan
    of every partition. The health endpoint is polled every few seconds by the
    dashboard; it gets the estimate the planner already maintains.
    """
    sql = text(
        """
        -- reltuples is -1 for a partition that has never been analyzed;
        -- GREATEST keeps a fresh, empty table from reporting a negative count.
        SELECT COALESCE(SUM(GREATEST(c.reltuples, 0)), 0)::BIGINT AS est
        FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = 'ticks'
        """
    )
    return int((await db.execute(sql)).scalar_one() or 0)


async def get_watermark(db: AsyncSession, name: str, default: datetime) -> datetime:
    row = await db.execute(
        text("SELECT value_ts FROM ingest_watermark WHERE name = :n"), {"n": name}
    )
    got = row.scalar_one_or_none()
    return got or default


async def set_watermark(db: AsyncSession, name: str, value: datetime) -> None:
    await db.execute(
        text(
            """
            INSERT INTO ingest_watermark (name, value_ts) VALUES (:n, :v)
            ON CONFLICT (name) DO UPDATE SET value_ts = EXCLUDED.value_ts, updated_at = now()
            """
        ),
        {"n": name, "v": value},
    )


async def log_ai_query(db: AsyncSession, **kw) -> None:
    await db.execute(
        text(
            """
            INSERT INTO ai_query_log
                (question, answer, tool_calls, input_tokens, output_tokens,
                 cost_usd, latency_ms, blocked_by, api_key_role)
            VALUES
                (:question, :answer, CAST(:tool_calls AS jsonb), :input_tokens, :output_tokens,
                 :cost_usd, :latency_ms, :blocked_by, :api_key_role)
            """
        ),
        kw,
    )


def utcnow() -> datetime:
    return datetime.now(UTC)


def default_window(hours: int = 24) -> tuple[datetime, datetime]:
    end = utcnow()
    return end - timedelta(hours=hours), end


__all__ = [n for n in dir() if not n.startswith("_")] + ["Decimal"]
