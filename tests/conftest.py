# PROMPT: Write pytest fixtures for a FastAPI async test suite backed by aiosqlite.
#   Need: (1) a session-scoped tmp_db fixture that points DB_PATH env var to an
#   isolated temp SQLite file so tests don't pollute each other; (2) an async
#   client fixture (function-scoped) using httpx.AsyncClient + ASGITransport
#   wired to the FastAPI app, that manually calls init_db() because ASGITransport
#   does not fire ASGI lifespan events. Framework: pytest-asyncio, aiosqlite,
#   httpx. Must be importable by all test files under tests/.
#
# CHANGES MADE: Scoped tmp_db as session-wide (not function) to keep one DB file
#   per test session; analytics tests use their own per-test tmp_path instead.
#   Added get_settings.cache_clear() call before constructing the client so env
#   var changes are reflected in the cached settings singleton.
import pytest
import pytest_asyncio
import os
from httpx import AsyncClient, ASGITransport


@pytest.fixture(scope="session", autouse=True)
def tmp_db(tmp_path_factory):
    """Point DB to a temp file so tests are isolated from each other."""
    db_file = tmp_path_factory.mktemp("db") / "test.db"
    os.environ["DB_PATH"] = str(db_file)
    return db_file


@pytest_asyncio.fixture
async def client(tmp_db):
    """
    AsyncClient wired to the FastAPI app, using the temp DB.

    httpx.ASGITransport does NOT send ASGI lifespan events, so we must
    call init_db() ourselves to ensure the schema exists before any test.
    """
    from core.config import get_settings
    get_settings.cache_clear()

    # Ensure DB_PATH env var is reflected in settings before importing app
    from main import app, init_db
    await init_db(str(tmp_db))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
