# PROMPT: Write pytest-asyncio tests for the GET /dashboard live web UI. Verify
#   it returns 200 HTML, embeds the configured default store, and ships the
#   multi-store selector wiring (a <select id="store-select">, a populateStores()
#   that reads /health, and a subscribeSSE() that re-subscribes per store). The
#   page is a single static HTML response, so assert on the served markup/JS.
#
# CHANGES MADE: Asserted on the selector + populateStores/subscribeSSE hooks
#   (not just status 200) so the test actually guards the multi-store feature
#   rather than just that the route exists. Pulled the expected default store
#   from settings so the test tracks config instead of hardcoding it.
"""
Tests for the live dashboard at GET /dashboard (multi-store selector).
"""
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_dashboard_returns_html(client: AsyncClient):
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in resp.text


@pytest.mark.asyncio
async def test_dashboard_embeds_default_store(client: AsyncClient):
    from core.config import get_settings
    store_id = get_settings().store_id
    resp = await client.get("/dashboard")
    # The placeholder must be substituted with the real configured store.
    assert "STORE_ID_PLACEHOLDER" not in resp.text
    assert store_id in resp.text


@pytest.mark.asyncio
async def test_dashboard_has_store_selector(client: AsyncClient):
    resp = await client.get("/dashboard")
    body = resp.text
    # Multi-store wiring: selector element + population from /health + per-store SSE.
    assert 'id="store-select"' in body
    assert "populateStores" in body
    assert "subscribeSSE" in body
    assert "/health" in body
