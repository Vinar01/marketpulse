"""Historical backfill from the exchange REST API.

Why this exists: an index benchmark on 4,000 rows measures nothing. Claims like
"the composite index cut p95 from 240ms to 3ms" are only meaningful against a
table large enough that the planner's choice actually matters, and waiting three
weeks for the live stream to produce that is not a plan.

Two modes, both real exchange data -- nothing here is synthetic:

  klines  1-minute candles straight into ohlcv_1m. 1,000 candles per request, so
          one symbol-year is ~525 requests. Ten symbols x 1 year is ~5M rows in
          a few minutes. This is what makes the OHLCV queries interesting.

  trades  Raw aggregated trades into ticks. Denser and slower (BTCUSDT alone
          produces 1-2M trades/day), so it is used for a short recent window --
          a couple of days across a few symbols is several million real ticks,
          which is what the tick-index benchmark runs against.

Usage:
    python -m app.ingest.backfill klines --days 365
    python -m app.ingest.backfill trades --hours 12 --symbols BTCUSDT,ETHUSDT
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.db import dispose_engines, session
from app.core.logging import configure_logging, get_logger
from app.core.tables import ohlcv_table
from app.ingest.writer import write_batch

log = get_logger(__name__)

KLINE_LIMIT = 1000
TRADE_LIMIT = 1000
# The exchange rejects aggTrades windows wider than one hour.
TRADE_WINDOW = timedelta(minutes=55)


class ExchangeClient:
    """Thin REST client with a concurrency cap and 429-aware retries.

    The semaphore is the important part: firing 500 requests at once gets the
    IP banned, not rate limited. Bounded concurrency plus honouring Retry-After
    keeps the whole backfill inside the published weight budget.
    """

    def __init__(self, concurrency: int = 4):
        self._sem = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(
            base_url=settings.binance_rest_url,
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "marketpulse-backfill/1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict, attempts: int = 5) -> list:
        for attempt in range(attempts):
            async with self._sem:
                try:
                    r = await self._client.get(path, params=params)
                except httpx.RequestError as exc:
                    if attempt == attempts - 1:
                        raise
                    log.warning("http_error_retrying", error=str(exc)[:120])
                    await asyncio.sleep(2**attempt)
                    continue

            if r.status_code == 200:
                return r.json()

            if r.status_code in (418, 429):
                # 429 = rate limited, 418 = you ignored a 429 and are now banned.
                wait = float(r.headers.get("Retry-After", 2**attempt))
                log.warning("rate_limited_backing_off", status=r.status_code, wait_s=wait)
                await asyncio.sleep(wait)
                continue

            if 500 <= r.status_code < 600:
                await asyncio.sleep(2**attempt)
                continue

            r.raise_for_status()

        raise RuntimeError(f"exchange request failed after {attempts} attempts: {path}")


# ---------------------------------------------------------------------------
# klines -> ohlcv_1m
# ---------------------------------------------------------------------------
def _kline_rows(symbol: str, raw: list) -> list[dict]:
    rows = []
    for k in raw:
        rows.append(
            {
                "symbol": symbol,
                "bucket": datetime.fromtimestamp(k[0] / 1000, tz=UTC),
                "open": Decimal(k[1]),
                "high": Decimal(k[2]),
                "low": Decimal(k[3]),
                "close": Decimal(k[4]),
                "volume": Decimal(k[5]),
                "quote_volume": Decimal(k[7]),
                "trade_count": int(k[8]),
            }
        )
    return rows


async def _upsert_ohlcv(rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = pg_insert(ohlcv_table).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol", "bucket"],
        set_={
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low": stmt.excluded.low,
            "close": stmt.excluded.close,
            "volume": stmt.excluded.volume,
            "quote_volume": stmt.excluded.quote_volume,
            "trade_count": stmt.excluded.trade_count,
        },
    )
    async with session() as db:
        await db.execute(stmt)
        await db.commit()
    return len(rows)


async def backfill_klines(client: ExchangeClient, symbol: str, days: int) -> int:
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    cursor = start
    total = 0

    while cursor < end:
        raw = await client.get(
            "/api/v3/klines",
            {
                "symbol": symbol,
                "interval": "1m",
                "startTime": int(cursor.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
                "limit": KLINE_LIMIT,
            },
        )
        if not raw:
            break

        rows = _kline_rows(symbol, raw)
        total += await _upsert_ohlcv(rows)
        # Advance past the last candle we received. Using the response rather
        # than a fixed step means a gap in exchange history cannot loop forever.
        cursor = rows[-1]["bucket"] + timedelta(minutes=1)

        if total % 20_000 < KLINE_LIMIT:
            log.info("klines_progress", symbol=symbol, rows=total, at=cursor.isoformat())

        if len(raw) < KLINE_LIMIT:
            break

    log.info("klines_done", symbol=symbol, rows=total)
    return total


# ---------------------------------------------------------------------------
# aggTrades -> ticks
# ---------------------------------------------------------------------------
def _trade_rows(symbol: str, raw: list) -> list[dict]:
    rows = []
    for t in raw:
        price = Decimal(t["p"])
        qty = Decimal(t["q"])
        rows.append(
            {
                "symbol": symbol,
                "trade_id": int(t["a"]),
                "price": price,
                "qty": qty,
                "quote_qty": price * qty,
                "is_buyer_maker": bool(t["m"]),
                "ts": datetime.fromtimestamp(t["T"] / 1000, tz=UTC),
            }
        )
    return rows


async def backfill_trades(client: ExchangeClient, symbol: str, hours: float) -> int:
    end = datetime.now(UTC)
    cursor = end - timedelta(hours=hours)
    total = 0

    while cursor < end:
        window_end = min(cursor + TRADE_WINDOW, end)
        raw = await client.get(
            "/api/v3/aggTrades",
            {
                "symbol": symbol,
                "startTime": int(cursor.timestamp() * 1000),
                "endTime": int(window_end.timestamp() * 1000),
                "limit": TRADE_LIMIT,
            },
        )

        if not raw:
            cursor = window_end
            continue

        rows = _trade_rows(symbol, raw)
        total += await write_batch(rows)

        if len(raw) < TRADE_LIMIT:
            cursor = window_end
        else:
            # The window held more than one page; resume one millisecond after
            # the last trade rather than skipping the remainder of the window.
            cursor = rows[-1]["ts"] + timedelta(milliseconds=1)

        if total % 50_000 < TRADE_LIMIT:
            log.info("trades_progress", symbol=symbol, rows=total, at=cursor.isoformat())

    log.info("trades_done", symbol=symbol, rows=total)
    return total


async def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="Backfill historical market data")
    parser.add_argument("mode", choices=["klines", "trades"])
    parser.add_argument("--symbols", default=",".join(settings.symbols))
    parser.add_argument("--days", type=int, default=30, help="klines mode: days of history")
    parser.add_argument("--hours", type=float, default=6, help="trades mode: hours of history")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    client = ExchangeClient(args.concurrency)
    started = time.perf_counter()
    total = 0

    try:
        if args.mode == "klines":
            results = await asyncio.gather(
                *(backfill_klines(client, s, args.days) for s in symbols)
            )
        else:
            # Trades are heavy; run symbols sequentially so one backfill cannot
            # saturate the write path the live worker is also using.
            results = []
            for s in symbols:
                results.append(await backfill_trades(client, s, args.hours))
        total = sum(results)
    finally:
        await client.aclose()
        await dispose_engines()

    elapsed = time.perf_counter() - started
    log.info(
        "backfill_complete",
        mode=args.mode,
        symbols=len(symbols),
        rows=total,
        seconds=round(elapsed, 1),
        rows_per_sec=round(total / elapsed) if elapsed else 0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
