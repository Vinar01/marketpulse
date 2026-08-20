"""Test fixtures.

The important one is `reset_pooled_connections`. pytest-asyncio gives each test
its own event loop, but the SQLAlchemy engine and Redis client are module-level
singletons that cache connections bound to the loop that created them. Reusing
one across tests means the second test inherits sockets attached to a loop that
has already been closed, which surfaces as a confusing "Event loop is closed".
Disposing them between tests costs a few milliseconds and removes the whole
class of failure.
"""
import os

import pytest

os.environ.setdefault("ENV", "test")


@pytest.fixture(autouse=True)
async def reset_pooled_connections():
    yield
    from app.core.cache import close_redis
    from app.core.db import dispose_engines

    await dispose_engines()
    await close_redis()
