"""Two engines, two privilege levels.

`engine` is the read/write application pool. `engine_ro` connects as a role that
holds SELECT and nothing else -- it is the only engine the AI tool layer can see.
Separating them at the connection level means a bug in the AI code cannot write,
regardless of what the model asks for.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

_engine: AsyncEngine | None = None
_engine_ro: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_sessionmaker_ro: async_sessionmaker[AsyncSession] | None = None


def _build(
    url: str, statement_timeout_ms: int, pool_size: int, read_only: bool = False
) -> AsyncEngine:
    server_settings = {
        # A runaway query cannot pin a connection forever. This is
        # Guardrail Layer 3 for the read-only engine.
        "statement_timeout": str(statement_timeout_ms),
        "application_name": "marketpulse-ai" if read_only else "marketpulse",
    }
    if read_only:
        # Belt and braces for Layer 4. The dedicated role already lacks write
        # privileges, but managed Postgres does not always grant CREATEROLE, so
        # the role may not exist and DATABASE_URL_RO may point at the app user.
        # This setting makes every transaction on this pool read-only regardless
        # of which role authenticated, so the guarantee does not depend on a
        # migration having succeeded.
        server_settings["default_transaction_read_only"] = "on"

    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=False,
        connect_args={"server_settings": server_settings},
    )


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = _build(settings.database_url, settings.db_statement_timeout_ms, settings.db_pool_size)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine_ro() -> AsyncEngine:
    global _engine_ro, _sessionmaker_ro
    if _engine_ro is None:
        _engine_ro = _build(
            settings.database_url_ro, settings.db_ro_statement_timeout_ms, 5, read_only=True
        )
        _sessionmaker_ro = async_sessionmaker(_engine_ro, expire_on_commit=False)
    return _engine_ro


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as s:
        yield s


@asynccontextmanager
async def session_ro() -> AsyncIterator[AsyncSession]:
    get_engine_ro()
    assert _sessionmaker_ro is not None
    async with _sessionmaker_ro() as s:
        yield s


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session() as s:
        yield s


async def get_session_ro() -> AsyncIterator[AsyncSession]:
    async with session_ro() as s:
        yield s


async def dispose_engines() -> None:
    global _engine, _engine_ro
    if _engine is not None:
        await _engine.dispose()
        _engine = None
    if _engine_ro is not None:
        await _engine_ro.dispose()
        _engine_ro = None
