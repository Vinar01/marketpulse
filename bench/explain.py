"""Index benchmark: measure the claim instead of asserting it.

Runs the platform's real hot queries with EXPLAIN (ANALYZE, BUFFERS) in three
configurations, against whatever data is actually in the database:

    1. baseline    indexes as they ship
    2. without     the composite index dropped
    3. restored    index rebuilt, re-measured to prove the delta was the index

Each query runs a warmup pass (so the comparison is not "cold cache vs warm
cache") and then N timed passes; the reported figure is the median.

    python -m bench.explain --runs 7
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.db import dispose_engines, session
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)

DROP_INDEX = "DROP INDEX IF EXISTS idx_ticks_symbol_ts"
CREATE_INDEX = "CREATE INDEX idx_ticks_symbol_ts ON ticks (symbol, ts DESC)"


def _queries(now: datetime) -> list[dict]:
    day_ago = now - timedelta(days=1)
    hour_ago = now - timedelta(hours=1)
    return [
        {
            "name": "latest_price",
            "why": "Hottest route in the product: newest tick for one symbol.",
            "sql": """
                SELECT symbol, price, qty, ts FROM ticks
                WHERE symbol = :symbol ORDER BY ts DESC LIMIT 1
            """,
            "params": {"symbol": "BTCUSDT"},
        },
        {
            "name": "tick_range_1h",
            "why": "Tick history page: one symbol, bounded window, newest first.",
            "sql": """
                SELECT symbol, trade_id, price, qty, ts FROM ticks
                WHERE symbol = :symbol AND ts >= :start AND ts < :end
                ORDER BY ts DESC, trade_id DESC LIMIT 500
            """,
            "params": {"symbol": "BTCUSDT", "start": hour_ago, "end": now},
        },
        {
            "name": "tick_range_aggregate_1h",
            "why": "Analytics shape: aggregate rather than fetch, so no LIMIT shortcut.",
            "sql": """
                SELECT count(*) AS trades, avg(price) AS avg_price, sum(qty) AS volume
                FROM ticks
                WHERE symbol = :symbol AND ts >= :start AND ts < :end
            """,
            "params": {"symbol": "BTCUSDT", "start": hour_ago, "end": now},
        },
        {
            "name": "ohlcv_day",
            "why": "Chart load: 1440 candles for one symbol (separate table, own index).",
            "sql": """
                SELECT bucket, open, high, low, close, volume FROM ohlcv_1m
                WHERE symbol = :symbol AND bucket >= :start AND bucket < :end
                ORDER BY bucket
            """,
            "params": {"symbol": "BTCUSDT", "start": day_ago, "end": now},
        },
    ]


async def _explain(db, sql: str, params: dict) -> tuple[float, str, int]:
    plan = await db.execute(text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"), params)
    raw = plan.scalar_one()
    doc = raw[0] if isinstance(raw, list) else json.loads(raw)[0]
    node = doc["Plan"]

    scan_types: list[str] = []

    def walk(n):
        scan_types.append(n["Node Type"])
        for child in n.get("Plans", []):
            walk(child)

    walk(node)
    shared_read = node.get("Shared Read Blocks", 0) + node.get("Shared Hit Blocks", 0)
    return doc["Execution Time"], " -> ".join(dict.fromkeys(scan_types)), shared_read


async def _time_query(db, q: dict, runs: int) -> dict:
    await db.execute(text(q["sql"]), q["params"])          # warmup
    samples, plan_desc, blocks = [], "", 0
    for _ in range(runs):
        started = time.perf_counter()
        await db.execute(text(q["sql"]), q["params"])
        samples.append((time.perf_counter() - started) * 1000)
    exec_ms, plan_desc, blocks = await _explain(db, q["sql"], q["params"])
    return {
        "median_ms": round(statistics.median(samples), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
        "planner_exec_ms": round(exec_ms, 3),
        "plan": plan_desc,
        "buffers": blocks,
    }


async def _row_counts(db) -> dict:
    ticks = (await db.execute(text("SELECT count(*) FROM ticks"))).scalar_one()
    ohlcv = (await db.execute(text("SELECT count(*) FROM ohlcv_1m"))).scalar_one()
    parts = (
        await db.execute(
            text(
                """SELECT count(*) FROM pg_class c
                   JOIN pg_inherits i ON i.inhrelid=c.oid
                   JOIN pg_class p ON p.oid=i.inhparent WHERE p.relname='ticks'"""
            )
        )
    ).scalar_one()
    return {"ticks": ticks, "ohlcv_1m": ohlcv, "tick_partitions": parts}


def _fmt(v) -> str:
    return f"{v:,}" if isinstance(v, int) else str(v)


async def main() -> int:
    configure_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=7)
    ap.add_argument("--markdown", action="store_true", help="emit a Markdown report")
    args = ap.parse_args()

    now = datetime.now(UTC)
    queries = _queries(now)

    async with session() as db:
        counts = await _row_counts(db)
        print(f"\ndataset: {_fmt(counts['ticks'])} ticks across "
              f"{counts['tick_partitions']} partitions, "
              f"{_fmt(counts['ohlcv_1m'])} OHLCV candles\n")

        # ANALYZE first: the planner chooses from statistics, and a benchmark
        # against stale statistics measures the statistics, not the index.
        await db.execute(text("ANALYZE ticks"))
        await db.execute(text("ANALYZE ohlcv_1m"))
        await db.commit()

        print("=== 1. baseline (indexes as shipped) ===")
        baseline = {q["name"]: await _time_query(db, q, args.runs) for q in queries}
        for name, r in baseline.items():
            print(f"  {name:26s} {r['median_ms']:9.3f} ms   {r['plan']}")

        print("\n=== 2. without idx_ticks_symbol_ts ===")
        await db.execute(text(DROP_INDEX))
        await db.commit()
        await db.execute(text("ANALYZE ticks"))
        await db.commit()
        without = {q["name"]: await _time_query(db, q, args.runs) for q in queries}
        for name, r in without.items():
            print(f"  {name:26s} {r['median_ms']:9.3f} ms   {r['plan']}")

        print("\n=== 3. index restored ===")
        await db.execute(text(CREATE_INDEX))
        await db.commit()
        await db.execute(text("ANALYZE ticks"))
        await db.commit()
        restored = {q["name"]: await _time_query(db, q, args.runs) for q in queries}
        for name, r in restored.items():
            print(f"  {name:26s} {r['median_ms']:9.3f} ms   {r['plan']}")

    print("\n=== summary ===")
    rows = []
    for q in queries:
        n = q["name"]
        b, w = baseline[n]["median_ms"], without[n]["median_ms"]
        speedup = (w / b) if b > 0 else float("inf")
        rows.append((n, b, w, speedup, baseline[n]["plan"], without[n]["plan"], q["why"]))
        print(f"  {n:26s} with {b:9.3f} ms | without {w:9.3f} ms | {speedup:7.1f}x")

    if args.markdown:
        out = ["", "| query | with index | without index | speedup | plan (with) | plan (without) |",
               "|---|---:|---:|---:|---|---|"]
        for n, b, w, s, pb, pw, _ in rows:
            out.append(f"| `{n}` | {b:.3f} ms | {w:.3f} ms | **{s:.1f}x** | {pb} | {pw} |")
        out.append("")
        out.append(f"Dataset: {_fmt(counts['ticks'])} ticks across "
                   f"{counts['tick_partitions']} partitions, "
                   f"{_fmt(counts['ohlcv_1m'])} OHLCV candles.")
        print("\n".join(out))

    await dispose_engines()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
