"""Live price WebSocket for the dashboard.

Fan-out matters here. The naive version gives every connected browser its own
polling loop, so 100 dashboards means 100 identical queries a second. Instead one
background task polls once and pushes the same snapshot to every subscriber:
database load is O(1) in the number of viewers instead of O(n).

Each subscriber gets a small bounded queue. If a client is too slow to drain it
(a backgrounded tab, a dying connection) we drop that client's oldest frame
rather than letting one stalled socket apply backpressure to the broadcaster and
stall everybody else.
"""
from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from app.core.config import settings
from app.core.db import session
from app.core.logging import get_logger
from app.core.metrics import WS_CONNECTIONS
from app.repository import latest_prices

log = get_logger(__name__)
router = APIRouter(tags=["realtime"])

BROADCAST_INTERVAL_S = 1.0
SUBSCRIBER_QUEUE_SIZE = 4


class PriceBroadcaster:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._last: list[dict] = []

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="price-broadcaster")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        if self._last:
            q.put_nowait(self._last)  # new client sees a price immediately
        self._subscribers.add(q)
        WS_CONNECTIONS.set(len(self._subscribers))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
        WS_CONNECTIONS.set(len(self._subscribers))

    async def _run(self) -> None:
        while True:
            try:
                async with session() as db:
                    rows = await latest_prices(db, settings.symbols)

                payload = [
                    {
                        "symbol": r["symbol"],
                        "price": str(r["price"]),
                        "qty": str(r["qty"]),
                        "ts": r["ts"].isoformat(),
                    }
                    for r in rows
                ]
                self._last = payload

                for q in list(self._subscribers):
                    if q.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            q.get_nowait()   # evict the stalest frame
                    with contextlib.suppress(asyncio.QueueFull):
                        q.put_nowait(payload)

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("broadcast_tick_failed", error=str(exc)[:200])

            await asyncio.sleep(BROADCAST_INTERVAL_S)


broadcaster = PriceBroadcaster()


@router.websocket("/ws/prices")
async def ws_prices(websocket: WebSocket, api_key: str = Query(default="")):
    """Live prices for all configured symbols.

    Browsers cannot set headers on a WebSocket handshake, so the API key comes in
    as a query parameter here. That is a deliberate, documented exception to the
    header-based auth used everywhere else -- not an oversight.
    """
    if api_key not in settings.api_key_map:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid api key")
        return

    await websocket.accept()
    queue = broadcaster.subscribe()
    log.info("ws_client_connected", subscribers=len(broadcaster._subscribers))

    try:
        while True:
            payload = await queue.get()
            await websocket.send_json({"type": "prices", "data": payload})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.info("ws_client_error", error=str(exc)[:120])
    finally:
        broadcaster.unsubscribe(queue)
        log.info("ws_client_disconnected", subscribers=len(broadcaster._subscribers))
