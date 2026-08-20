"""Periodic jobs: 1-minute rollup, partition creation, retention.

The rollup is watermark-driven rather than "recompute the last hour every time".
The watermark records how far the aggregator has folded; each pass processes
[watermark - overlap, now - grace) and moves it forward. The overlap re-covers
the boundary so a trade that arrived late still lands in its candle, and the
grace period stops us from sealing the current minute while trades are still
arriving for it.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.config import settings
from app.core.db import session
from app.core.logging import get_logger
from app.core.metrics import AGG_ROWS_UPSERTED, AGG_RUN_SECONDS
from app.repository import get_watermark, set_watermark

log = get_logger(__name__)

WATERMARK = "ohlcv_1m"
# Re-fold the two minutes behind the watermark on every pass, so a trade that
# arrived after its minute was first rolled up still gets counted.
OVERLAP = timedelta(minutes=2)
# Never seal the minute that is still in progress.
GRACE = timedelta(seconds=15)


async def run_aggregation_once() -> int:
    now = datetime.now(UTC)
    end = (now - GRACE).replace(second=0, microsecond=0)

    async with session() as db:
        default_start = end - timedelta(minutes=10)
        watermark = await get_watermark(db, WATERMARK, default_start)
        start = min(watermark - OVERLAP, end)

        if start >= end:
            return 0

        started = time.perf_counter()
        rows = await db.execute(
            text("SELECT aggregate_ohlcv_1m(:start, :end)"), {"start": start, "end": end}
        )
        affected = int(rows.scalar_one() or 0)
        await set_watermark(db, WATERMARK, end)
        await db.commit()

    AGG_RUN_SECONDS.observe(time.perf_counter() - started)
    AGG_ROWS_UPSERTED.inc(affected)
    if affected:
        log.info("aggregated", rows=affected, window_start=start.isoformat(), window_end=end.isoformat())
    return affected


async def run_maintenance_once() -> dict:
    async with session() as db:
        created = await db.execute(
            text("SELECT ensure_tick_partitions(:ahead, 1)"),
            {"ahead": settings.partition_ahead_days},
        )
        created_n = int(created.scalar_one() or 0)
        dropped = await db.execute(
            text("SELECT drop_old_tick_partitions(:keep)"), {"keep": settings.retention_days}
        )
        dropped_n = int(dropped.scalar_one() or 0)
        await db.commit()

    if created_n or dropped_n:
        log.info("partitions_maintained", created=created_n, dropped=dropped_n)
    return {"created": created_n, "dropped": dropped_n}


class Aggregator:
    """Runs the rollup on a short timer and maintenance on a long one."""

    def __init__(self) -> None:
        self.interval = settings.aggregator_interval_s
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("aggregator_starting", interval_s=self.interval)
        await self._safe(run_maintenance_once, "maintenance")
        last_maintenance = time.monotonic()

        while not self._stop.is_set():
            await self._safe(run_aggregation_once, "aggregation")

            if time.monotonic() - last_maintenance > 3600:
                await self._safe(run_maintenance_once, "maintenance")
                last_maintenance = time.monotonic()

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass

        log.info("aggregator_stopped")

    @staticmethod
    async def _safe(fn, label: str):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            log.error("periodic_job_failed", job=label, error=str(exc)[:300])
            return None
