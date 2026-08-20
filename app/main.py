"""FastAPI application: middleware, lifespan, router wiring."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import routes_ai, routes_market, routes_ops, routes_ws
from app.api.routes_ws import broadcaster
from app.core.cache import close_redis, rate_limit_hit
from app.core.config import settings
from app.core.db import dispose_engines, get_engine
from app.core.logging import configure_logging, get_logger
from app.core.metrics import API_LATENCY, API_RATE_LIMITED, API_REQUESTS

log = get_logger(__name__)

# Routes that must answer even when the caller is over their limit, or the
# monitoring that would tell you about the outage goes dark during the outage.
RATE_LIMIT_EXEMPT = {"/health", "/health/live", "/health/ready", "/metrics", "/docs", "/openapi.json"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    get_engine()
    await broadcaster.start()
    log.info("api_started", version=__version__, env=settings.env, symbols=settings.symbols)
    yield
    await broadcaster.stop()
    await close_redis()
    await dispose_engines()
    log.info("api_stopped")


app = FastAPI(
    title="MarketPulse",
    version=__version__,
    description=(
        "Real-time crypto market-data platform: async ingestion, partitioned "
        "PostgreSQL, Redis caching, and a guardrailed AI query layer."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _route_template(request: Request) -> str:
    """Group metrics by route template, not by URL.

    Labelling with the raw path would create a distinct time series per symbol
    per cursor -- unbounded cardinality, and the fastest way to take down a
    Prometheus server.
    """
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


@app.middleware("http")
async def observe_and_limit(request: Request, call_next):
    started = time.perf_counter()
    path = request.url.path

    if path not in RATE_LIMIT_EXEMPT:
        # Identity is the API key when present, else the peer address, so an
        # unauthenticated flood is still bounded.
        api_key = request.headers.get("X-API-Key")
        identity = f"key:{api_key}" if api_key else f"ip:{request.client.host if request.client else 'unknown'}"

        allowed, _remaining, reset_in = await rate_limit_hit(
            identity, settings.rate_limit_per_minute
        )
        if not allowed:
            API_RATE_LIMITED.inc()
            API_REQUESTS.labels(method=request.method, path=path, status="429").inc()
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
                headers={
                    "Retry-After": str(reset_in),
                    "X-RateLimit-Limit": str(settings.rate_limit_per_minute),
                    "X-RateLimit-Remaining": "0",
                },
            )

    try:
        response = await call_next(request)
    except Exception:
        API_REQUESTS.labels(method=request.method, path=path, status="500").inc()
        raise

    elapsed = time.perf_counter() - started
    template = _route_template(request)
    API_LATENCY.labels(method=request.method, path=template).observe(elapsed)
    API_REQUESTS.labels(
        method=request.method, path=template, status=str(response.status_code)
    ).inc()
    response.headers["X-Response-Time-ms"] = f"{elapsed * 1000:.1f}"

    if response.status_code >= 400 or elapsed > 1.0:
        log.info(
            "request",
            method=request.method,
            path=path,
            status=response.status_code,
            ms=round(elapsed * 1000, 1),
        )
    return response


app.include_router(routes_ops.router)
app.include_router(routes_market.router)
app.include_router(routes_ai.router)
app.include_router(routes_ws.router)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "marketpulse",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
        "metrics": "/metrics",
    }
