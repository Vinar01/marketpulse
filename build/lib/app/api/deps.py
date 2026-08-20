"""Shared request dependencies: authentication, authorisation, validated params."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import APIKeyHeader

from app.core.config import settings
from app.schemas import validate_symbol_format

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

MAX_WINDOW = timedelta(days=31)


@dataclass(frozen=True)
class Principal:
    key: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


async def require_api_key(
    request: Request, api_key: Annotated[str | None, Depends(api_key_header)]
) -> Principal:
    """Authentication.

    Static keys are the right weight for an internal ops tool: no login flow, no
    token refresh, revocation is a config change. The key maps to a role, and
    roles gate the expensive routes -- the AI endpoint costs real money per call,
    so it is not something an unauthenticated caller gets to trigger.
    """
    if not api_key or api_key not in settings.api_key_map:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid X-API-Key",
            headers={"WWW-Authenticate": "APIKey"},
        )
    principal = Principal(key=api_key, role=settings.api_key_map[api_key])
    request.state.principal = principal
    return principal


async def require_admin(
    principal: Annotated[Principal, Depends(require_api_key)],
) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return principal


async def valid_symbol(symbol: str) -> str:
    """Path-parameter symbol validation (Guardrail Layer 1)."""
    try:
        return validate_symbol_format(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime


async def time_window(
    start: Annotated[datetime | None, Query(description="ISO-8601 UTC, inclusive")] = None,
    end: Annotated[datetime | None, Query(description="ISO-8601 UTC, exclusive")] = None,
    hours: Annotated[int | None, Query(ge=1, le=744, description="shorthand: last N hours")] = None,
) -> TimeWindow:
    """Resolve and bound a query window.

    An unbounded range is a denial-of-service vector dressed up as a feature:
    `?start=1970-01-01` asks the database to scan every partition. Capping the
    span at 31 days keeps every query inside a handful of partitions.
    """
    now = datetime.now(UTC)

    if hours is not None:
        return TimeWindow(start=now - timedelta(hours=hours), end=now)

    resolved_end = end or now
    resolved_start = start or (resolved_end - timedelta(hours=24))

    if resolved_start.tzinfo is None:
        resolved_start = resolved_start.replace(tzinfo=UTC)
    if resolved_end.tzinfo is None:
        resolved_end = resolved_end.replace(tzinfo=UTC)

    if resolved_start >= resolved_end:
        raise HTTPException(status_code=422, detail="start must be strictly before end")
    if resolved_end - resolved_start > MAX_WINDOW:
        raise HTTPException(
            status_code=422,
            detail=f"window too large: maximum {MAX_WINDOW.days} days per request",
        )
    return TimeWindow(start=resolved_start, end=resolved_end)


async def page_limit(
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> int:
    return min(limit, settings.max_page_size)
