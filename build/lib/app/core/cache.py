"""Redis with two jobs, both of which it is uniquely good at.

1. Hot-price cache (cache-aside). The latest-price endpoint is the most-hit
   route in the product and its answer changes at most once per tick; a 2s TTL
   turns thousands of identical index scans into one Redis GET.
2. Distributed rate limiting. A per-process counter is wrong the moment you run
   two API replicas behind a load balancer. Redis gives every replica the same
   counter, and INCR+EXPIRE is atomic in one round trip via a Lua script.

Redis is treated as strictly optional: every call degrades to the database on
failure and increments cache_errors_total. A cache outage must not be an outage.
"""
from __future__ import annotations

import orjson
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import CACHE_ERRORS, CACHE_HITS, CACHE_MISSES

log = get_logger(__name__)

_client: aioredis.Redis | None = None

# INCR the key, and set the TTL only when we created it (first hit of a window).
# Doing this in Lua makes the check-and-expire atomic, so a crash between the
# two commands cannot leave a key without a TTL and lock a caller out forever.
_RATE_LIMIT_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {current, ttl}
"""

_rate_limit_script = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=False,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def cache_get(key: str, kind: str) -> dict | list | None:
    try:
        raw = await get_redis().get(key)
    except Exception as exc:  # noqa: BLE001 - cache must never be fatal
        CACHE_ERRORS.inc()
        log.warning("cache_get_failed", key=key, error=str(exc))
        return None
    if raw is None:
        CACHE_MISSES.labels(key_kind=kind).inc()
        return None
    CACHE_HITS.labels(key_kind=kind).inc()
    return orjson.loads(raw)


async def cache_set(key: str, value, ttl: int) -> None:
    try:
        await get_redis().set(key, orjson.dumps(value), ex=ttl)
    except Exception as exc:  # noqa: BLE001
        CACHE_ERRORS.inc()
        log.warning("cache_set_failed", key=key, error=str(exc))


async def cache_invalidate(*keys: str) -> None:
    if not keys:
        return
    try:
        await get_redis().delete(*keys)
    except Exception as exc:  # noqa: BLE001
        CACHE_ERRORS.inc()
        log.warning("cache_invalidate_failed", error=str(exc))


async def rate_limit_hit(identity: str, limit: int, window_s: int = 60) -> tuple[bool, int, int]:
    """Return (allowed, remaining, reset_in_seconds).

    Fails open: if Redis is unreachable we serve the request rather than take the
    API down. That is the right trade for a rate limiter (availability over
    strictness); it would be the wrong trade for an authorization check.
    """
    global _rate_limit_script
    try:
        r = get_redis()
        if _rate_limit_script is None:
            _rate_limit_script = r.register_script(_RATE_LIMIT_LUA)
        key = f"rl:{identity}:{window_s}"
        current, ttl = await _rate_limit_script(keys=[key], args=[window_s])
        current, ttl = int(current), int(ttl)
        return current <= limit, max(0, limit - current), max(ttl, 0)
    except Exception as exc:  # noqa: BLE001
        CACHE_ERRORS.inc()
        log.warning("rate_limit_unavailable_failing_open", error=str(exc))
        return True, limit, window_s


async def redis_healthy() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception:  # noqa: BLE001
        return False
