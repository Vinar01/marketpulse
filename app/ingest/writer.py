"""Batching writer: many trades per transaction, not one transaction per trade.

A single-row INSERT costs a network round trip, a transaction begin/commit, and
a WAL flush. At a few thousand trades a second that is the entire bottleneck.
Batching amortises all three across N rows: at N=500 the per-row cost of the
round trip and the fsync drops by ~500x, and throughput goes from "cannot keep
up" to "idle most of the time".

The batch flushes on whichever comes first:
  * size  -- keeps memory and statement size bounded under load
  * time  -- keeps latency bounded when the market is quiet
Without the timer, a slow symbol's last few trades would sit in the buffer
indefinitely; without the size cap, a burst would build one enormous statement.
"""
from __future__ import annotations

import asyncio
import time

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.db import session
from app.core.logging import get_logger
from app.core.metrics import (
    DB_BATCH_SIZE,
    DB_WRITE_LATENCY,
    INGEST_QUEUE_DEPTH,
    INGEST_ROWS_DUPLICATE,
    INGEST_ROWS_WRITTEN,
)
from app.core.tables import ticks_table

log = get_logger(__name__)


async def write_batch(rows: list[dict]) -> int:
    """Insert a batch idempotently. Returns the number of rows actually stored.

    ON CONFLICT DO NOTHING on (symbol, trade_id, ts) makes replay safe. That
    matters because reconnects overlap: after a drop, the exchange resends recent
    trades, and a backfill can cover a window the live stream already wrote.
    Without idempotent writes, every reconnect would either duplicate trades or
    require a read-before-write; with it, the database enforces exactly-once
    storage and the ingest path stays a single statement.
    """
    if not rows:
        return 0

    stmt = pg_insert(ticks_table).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["symbol", "trade_id", "ts"])

    started = time.perf_counter()
    async with session() as db:
        result = await db.execute(stmt)
        await db.commit()
    elapsed = time.perf_counter() - started

    written = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else len(rows)
    duplicates = max(0, len(rows) - written)

    DB_WRITE_LATENCY.observe(elapsed)
    DB_BATCH_SIZE.observe(len(rows))
    INGEST_ROWS_WRITTEN.inc(written)
    if duplicates:
        INGEST_ROWS_DUPLICATE.inc(duplicates)

    return written


class BatchWriter:
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self.batch_size = settings.ingest_batch_size
        self.interval = settings.ingest_batch_interval_ms / 1000
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("writer_starting", batch_size=self.batch_size, interval_s=self.interval)
        buffer: list[dict] = []
        deadline = time.monotonic() + self.interval

        while not self._stop.is_set() or not self.queue.empty():
            timeout = max(0.0, deadline - time.monotonic())
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=timeout or 0.01)
                buffer.append(item)
            except TimeoutError:
                pass

            now = time.monotonic()
            if buffer and (len(buffer) >= self.batch_size or now >= deadline):
                await self._flush(buffer)
                buffer = []
                deadline = now + self.interval

            INGEST_QUEUE_DEPTH.set(self.queue.qsize())

        if buffer:
            await self._flush(buffer)
        log.info("writer_stopped")

    async def _flush(self, buffer: list[dict]) -> None:
        # Dedupe inside the batch too. The exchange can resend a trade within a
        # single window, and Postgres rejects an INSERT whose own VALUES list
        # contains the same conflict key twice ("cannot affect row a second
        # time") -- ON CONFLICT does not save you from a self-conflict.
        seen: set[tuple] = set()
        unique: list[dict] = []
        for row in buffer:
            key = (row["symbol"], row["trade_id"], row["ts"])
            if key not in seen:
                seen.add(key)
                unique.append(row)

        try:
            written = await write_batch(unique)
            log.debug("batch_written", rows=len(unique), stored=written)
        except Exception as exc:  # noqa: BLE001
            # Losing one batch is survivable and self-heals: the exchange replays
            # recent trades on reconnect and the backfill tool can close any gap.
            # Crashing the writer would lose every subsequent batch too.
            log.error("batch_write_failed", rows=len(unique), error=str(exc)[:300])
