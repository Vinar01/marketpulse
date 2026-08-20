"""Health, readiness and metrics.

Liveness and readiness are separate on purpose. /health/live answers "is this
process running" -- if it fails, restart the container. /health/ready answers
"can this process serve traffic" -- if it fails, take it out of the load balancer
but do NOT restart it, because a Postgres blip is not fixed by killing the API.
Conflating the two produces restart loops during dependency outages.
"""
from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository as repo
from app.core.cache import redis_healthy
from app.core.db import get_session
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["ops"])

DB = Annotated[AsyncSession, Depends(get_session)]
STARTED_AT = time.time()


@router.get("/health/live")
async def live():
    return {"status": "ok", "uptime_s": round(time.time() - STARTED_AT, 1)}


@router.get("/health/ready")
async def ready(db: DB, response: Response):
    checks: dict[str, str] = {}

    try:
        await db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["postgres"] = f"error: {str(exc)[:120]}"

    checks["redis"] = "ok" if await redis_healthy() else "degraded"

    # Redis being down is degraded, not unready: every cached route falls back
    # to Postgres, so the API still answers correctly, just more slowly.
    healthy = checks["postgres"] == "ok"
    response.status_code = 200 if healthy else 503
    return {"status": "ok" if healthy else "unready", "checks": checks}


@router.get("/health")
async def health(db: DB):
    """Rich status for the dashboard's system panel."""
    checks: dict[str, str] = {}
    ingest: dict = {}

    try:
        await db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
        stats = await repo.ingest_stats(db)
        ingest = {
            "tick_rows_estimate": await repo.approx_tick_count(db),
            "ohlcv_rows": stats["ohlcv_rows"],
            "last_tick_ts": stats["last_tick_ts"].isoformat() if stats["last_tick_ts"] else None,
            "last_bucket": stats["last_bucket"].isoformat() if stats["last_bucket"] else None,
            "tick_partitions": stats["tick_partitions"],
            "ticks_size": stats["ticks_size"],
        }
        if stats["last_tick_ts"]:
            age = (repo.utcnow() - stats["last_tick_ts"]).total_seconds()
            ingest["seconds_since_last_tick"] = round(age, 1)
            checks["ingest"] = "ok" if age < 60 else "stale"
        else:
            checks["ingest"] = "no data"
    except Exception as exc:  # noqa: BLE001
        checks["postgres"] = f"error: {str(exc)[:120]}"

    checks["redis"] = "ok" if await redis_healthy() else "degraded"

    from app import __version__

    return {
        "status": "ok" if checks.get("postgres") == "ok" else "degraded",
        "version": __version__,
        "uptime_s": round(time.time() - STARTED_AT, 1),
        "checks": checks,
        "ingest": ingest,
    }


@router.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint. Deliberately unauthenticated so a scraper
    inside the deployment network needs no credential; expose it on an internal
    port or behind a network policy in production."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
