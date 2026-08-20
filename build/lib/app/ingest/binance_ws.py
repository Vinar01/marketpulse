"""Exchange WebSocket reader.

Why WebSocket and not REST polling: a poll returns whatever the last snapshot
was, so you get one price per request and miss everything in between. Trades are
events, not state -- a push stream delivers each one exactly once, with the
exchange's own event timestamp, at a fraction of the request overhead.

Why asyncio and not threads: this task is I/O-bound and spends essentially all of
its life blocked on a socket read. Threads would cost an 8MB stack and a context
switch per symbol to do nothing; a coroutine costs a few hundred bytes and the
event loop wakes it only when bytes actually arrive. One process handles all
symbols on one connection.
"""
from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from decimal import Decimal

import orjson
import websockets

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import (
    INGEST_CONNECTED,
    INGEST_DROPPED,
    INGEST_LAG_SECONDS,
    INGEST_QUEUE_DEPTH,
    INGEST_RECONNECTS,
    INGEST_TRADES_TOTAL,
)

log = get_logger(__name__)

MAX_BACKOFF_S = 30.0
BASE_BACKOFF_S = 0.5


def stream_url(symbols: list[str]) -> str:
    streams = "/".join(f"{s.lower()}@aggTrade" for s in symbols)
    return f"{settings.binance_ws_url}?streams={streams}"


def parse_agg_trade(payload: dict) -> dict | None:
    """Map an aggTrade frame to a tick row.

    Binance sends numbers as strings precisely so clients do not lose precision;
    we keep that promise by parsing straight into Decimal. float(payload["p"])
    here would be a silent, permanent data-quality bug.
    """
    d = payload.get("data") or payload
    if d.get("e") != "aggTrade":
        return None
    try:
        price = Decimal(d["p"])
        qty = Decimal(d["q"])
        return {
            "symbol": d["s"],
            "trade_id": int(d["a"]),
            "price": price,
            "qty": qty,
            "quote_qty": price * qty,
            "is_buyer_maker": bool(d["m"]),
            "ts": datetime.fromtimestamp(d["T"] / 1000, tz=UTC),
        }
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        log.warning("unparseable_frame", error=str(exc))
        return None


class BinanceTradeStream:
    def __init__(self, symbols: list[str], queue: asyncio.Queue):
        self.symbols = symbols
        self.queue = queue
        self._stop = asyncio.Event()
        self._attempt = 0

    def stop(self) -> None:
        self._stop.set()

    def _backoff(self) -> float:
        """Exponential backoff with full jitter.

        Plain exponential backoff makes every disconnected client retry at the
        same instant after a shared outage, and the thundering herd knocks the
        endpoint over again the moment it recovers. Full jitter -- a uniform
        random draw from [0, cap] -- spreads the retries out and is what AWS
        recommends after measuring the alternatives.
        """
        cap = min(MAX_BACKOFF_S, BASE_BACKOFF_S * (2**self._attempt))
        return random.uniform(0, cap)

    async def run(self) -> None:
        url = stream_url(self.symbols)
        log.info("stream_starting", symbols=self.symbols)

        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,   # exchange drops idle sockets; keep it warm
                    ping_timeout=20,
                    max_queue=2**12,
                    close_timeout=5,
                ) as ws:
                    INGEST_CONNECTED.set(1)
                    self._attempt = 0
                    log.info("stream_connected", url=url[:80])
                    await self._consume(ws)

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                INGEST_CONNECTED.set(0)
                INGEST_RECONNECTS.inc()
                delay = self._backoff()
                self._attempt = min(self._attempt + 1, 8)
                log.warning(
                    "stream_disconnected_retrying",
                    error=str(exc)[:200],
                    retry_in_s=round(delay, 2),
                    attempt=self._attempt,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass

        INGEST_CONNECTED.set(0)
        log.info("stream_stopped")

    async def _consume(self, ws) -> None:
        async for raw in ws:
            if self._stop.is_set():
                return
            tick = parse_agg_trade(orjson.loads(raw))
            if tick is None:
                continue

            INGEST_TRADES_TOTAL.labels(symbol=tick["symbol"]).inc()
            lag = (datetime.now(UTC) - tick["ts"]).total_seconds()
            INGEST_LAG_SECONDS.labels(symbol=tick["symbol"]).set(lag)

            try:
                self.queue.put_nowait(tick)
            except asyncio.QueueFull:
                # Backpressure decision: shed, do not block.
                #
                # `await queue.put()` would stop reading the socket. The exchange
                # keeps sending regardless, the kernel buffer fills, and the
                # exchange eventually disconnects us for being slow -- so a brief
                # database hiccup would escalate into a full stream outage and a
                # much larger gap than the one we were trying to avoid.
                # Dropping the newest tick loses one trade and is visible in
                # ingest_dropped_total, which is a monitorable, bounded failure.
                INGEST_DROPPED.inc()

            INGEST_QUEUE_DEPTH.set(self.queue.qsize())
