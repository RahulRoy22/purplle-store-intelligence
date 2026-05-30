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
