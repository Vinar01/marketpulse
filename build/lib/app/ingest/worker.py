"""Ingestion worker entrypoint: stream -> queue -> batch writer, plus rollup.

    exchange WS ──▶ asyncio.Queue(bounded) ──▶ BatchWriter ──▶ Postgres
                                                   │
                              Aggregator ──────────┴──▶ ohlcv_1m

Run as its own process:  python -m app.ingest.worker

Shutdown order is deliberate. On SIGTERM we stop the *stream* first so nothing
new enters the queue, then let the writer drain what is already buffered, and
only then exit. Killing the writer first would discard every buffered trade.
"""
from __future__ import annotations

import asyncio
import signal

from prometheus_client import start_http_server

from app.core.config import settings
from app.core.db import dispose_engines, session
from app.core.logging import configure_logging, get_logger
from app.ingest.aggregator import Aggregator, run_maintenance_once
from app.ingest.binance_ws import BinanceTradeStream
from app.ingest.writer import BatchWriter
from app.repository import list_symbols

log = get_logger(__name__)

METRICS_PORT = 9101


async def _resolve_symbols() -> list[str]:
    """Only stream symbols that exist in the symbols table.

    ticks.symbol is a foreign key, so streaming an unregistered symbol would make
    every batch containing it fail. Intersecting up front turns a runtime write
    error into a startup log line.
    """
    async with session() as db:
        known = {r["symbol"] for r in await list_symbols(db)}
    wanted = [s for s in settings.symbols if s in known]
    missing = [s for s in settings.symbols if s not in known]
    if missing:
        log.warning("symbols_not_registered_skipping", symbols=missing)
    if not wanted:
        raise RuntimeError("no configured symbols exist in the symbols table; run scripts/migrate.py")
    return wanted


async def main() -> None:
    configure_logging()
    start_http_server(METRICS_PORT)
    log.info("worker_starting", metrics_port=METRICS_PORT)

    await run_maintenance_once()
    symbols = await _resolve_symbols()

    queue: asyncio.Queue = asyncio.Queue(maxsize=settings.ingest_queue_maxsize)
    stream = BinanceTradeStream(symbols, queue)
    writer = BatchWriter(queue)
    aggregator = Aggregator()

    stream_task = asyncio.create_task(stream.run(), name="stream")
    writer_task = asyncio.create_task(writer.run(), name="writer")
    agg_task = asyncio.create_task(aggregator.run(), name="aggregator")

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    await asyncio.wait(
        [asyncio.create_task(shutdown.wait()), stream_task, writer_task, agg_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    log.info("worker_draining")
    stream.stop()
    await asyncio.wait_for(stream_task, timeout=10)

    writer.stop()          # writer keeps going until the queue is empty
    aggregator.stop()
    await asyncio.wait_for(asyncio.gather(writer_task, agg_task, return_exceptions=True), timeout=30)

    await dispose_engines()
    log.info("worker_stopped", queue_remaining=queue.qsize())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
