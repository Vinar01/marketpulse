"""Market data endpoints."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository as repo
from app.api.deps import (
    Principal,
    TimeWindow,
    page_limit,
    require_api_key,
    time_window,
    valid_symbol,
)
from app.core.cache import cache_get, cache_set
from app.core.config import settings
from app.core.db import get_session
from app.schemas import CursorPage, LatestPriceOut, OHLCVOut, SymbolOut, TickOut

router = APIRouter(prefix="/api/v1", tags=["market"])

DB = Annotated[AsyncSession, Depends(get_session)]
Auth = Annotated[Principal, Depends(require_api_key)]


@router.get("/symbols", response_model=list[SymbolOut])
async def get_symbols(db: DB, _: Auth):
    return await repo.list_symbols(db)


@router.get("/prices/latest", response_model=list[LatestPriceOut])
async def latest_all(db: DB, _: Auth):
    """Latest price for every configured symbol. Powers the dashboard header."""
    key = "latest:all"
    cached = await cache_get(key, "latest_all")
    if cached is not None:
        return [LatestPriceOut(**{**row, "source": "cache"}) for row in cached]

    rows = await repo.latest_prices(db, settings.symbols)
    payload = [
        {
            "symbol": r["symbol"],
            "price": str(r["price"]),
            "qty": str(r["qty"]),
            "ts": r["ts"].isoformat(),
        }
        for r in rows
    ]
    await cache_set(key, payload, settings.cache_ttl_latest)
    return [LatestPriceOut(**{**row, "source": "database"}) for row in payload]


@router.get("/prices/latest/{symbol}", response_model=LatestPriceOut)
async def latest_one(symbol: Annotated[str, Depends(valid_symbol)], db: DB, _: Auth):
    """Cache-aside on the hottest route in the product.

        client ──▶ API ──▶ Redis ──hit──▶ response
                            │miss
                            └──▶ Postgres ──▶ Redis SET (ttl) ──▶ response

    The TTL is 2 seconds, not 2 minutes, and that is the whole design: a trader
    tolerates a 2-second-old price, and at that TTL a symbol quoted 1,000 times
    a second still costs the database one query per 2 seconds. No invalidation
    on write either -- with a TTL this short, an explicit invalidation from the
    ingest path would add coupling and a race for no measurable freshness gain.
    """
    key = f"latest:{symbol}"
    cached = await cache_get(key, "latest_one")
    if cached is not None:
        return LatestPriceOut(**{**cached, "source": "cache"})

    row = await repo.latest_price(db, symbol)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no ticks recorded for {symbol}")

    payload = {
        "symbol": row["symbol"],
        "price": str(row["price"]),
        "qty": str(row["qty"]),
        "ts": row["ts"].isoformat(),
    }
    await cache_set(key, payload, settings.cache_ttl_latest)
    return LatestPriceOut(**{**payload, "source": "database"})


@router.get("/prices/{symbol}", response_model=CursorPage)
async def price_history(
    symbol: Annotated[str, Depends(valid_symbol)],
    window: Annotated[TimeWindow, Depends(time_window)],
    limit: Annotated[int, Depends(page_limit)],
    db: DB,
    _: Auth,
    cursor: Annotated[str | None, Query(description="opaque cursor from next_cursor")] = None,
):
    """Tick history, newest first, keyset-paginated.

    Pass `next_cursor` back as `cursor` to get the following page. The cursor is
    an opaque encoding of the last row's (ts, trade_id); see CursorPage for why
    this beats OFFSET.
    """
    try:
        rows, next_cursor, has_more = await repo.get_ticks(
            db, symbol, window.start, window.end, limit, cursor
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return CursorPage(
        items=[TickOut(**r) for r in rows], next_cursor=next_cursor, has_more=has_more
    )


@router.get("/ohlcv/{symbol}", response_model=list[OHLCVOut])
async def ohlcv(
    symbol: Annotated[str, Depends(valid_symbol)],
    window: Annotated[TimeWindow, Depends(time_window)],
    limit: Annotated[int, Depends(page_limit)],
    db: DB,
    _: Auth,
):
    key = f"ohlcv:{symbol}:{int(window.start.timestamp())}:{int(window.end.timestamp())}:{limit}"
    cached = await cache_get(key, "ohlcv")
    if cached is not None:
        return [OHLCVOut(**row) for row in cached]

    rows = await repo.get_ohlcv(db, symbol, window.start, window.end, limit)
    payload = [
        {
            **{k: str(v) for k, v in r.items() if k in
               {"open", "high", "low", "close", "volume", "quote_volume"}},
            "symbol": r["symbol"],
            "bucket": r["bucket"].isoformat(),
            "trade_count": r["trade_count"],
        }
        for r in rows
    ]
    await cache_set(key, payload, settings.cache_ttl_ohlcv)
    return [OHLCVOut(**row) for row in payload]


@router.get("/market/summary")
async def market_summary(
    window: Annotated[TimeWindow, Depends(time_window)],
    db: DB,
    _: Auth,
):
    """Cross-symbol leaderboard: return, volatility and volume over the window."""
    rows = await repo.compare_symbols(db, settings.symbols, window.start, window.end)
    return {
        "window": {"start": window.start.isoformat(), "end": window.end.isoformat()},
        "symbols": rows,
    }
