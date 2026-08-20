"""API integration tests. These hit the real database -- run scripts/migrate.py first."""
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

KEY = {"X-API-Key": "demo-key-public"}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_reports_dependencies(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "postgres" in body["checks"]
    assert "redis" in body["checks"]


async def test_liveness_needs_no_dependencies(client):
    assert (await client.get("/health/live")).status_code == 200


async def test_metrics_exposes_prometheus_format(client):
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "api_requests_total" in r.text


async def test_market_routes_require_a_key(client):
    for path in ("/api/v1/symbols", "/api/v1/prices/latest", "/api/v1/ohlcv/BTCUSDT"):
        assert (await client.get(path)).status_code == 401, path


async def test_bad_key_is_rejected(client):
    r = await client.get("/api/v1/symbols", headers={"X-API-Key": "not-a-real-key"})
    assert r.status_code == 401


async def test_symbols_returns_seeded_rows(client):
    r = await client.get("/api/v1/symbols", headers=KEY)
    assert r.status_code == 200
    assert any(s["symbol"] == "BTCUSDT" for s in r.json())


async def test_symbol_format_is_validated_at_the_edge(client):
    r = await client.get("/api/v1/ohlcv/BTC';DROP TABLE ticks;--", headers=KEY)
    assert r.status_code == 422


async def test_window_larger_than_the_cap_is_rejected(client):
    r = await client.get(
        "/api/v1/ohlcv/BTCUSDT",
        params={"start": "1970-01-01T00:00:00Z", "end": "2030-01-01T00:00:00Z"},
        headers=KEY,
    )
    assert r.status_code == 422
    assert "window too large" in r.json()["detail"]


async def test_reversed_window_is_rejected(client):
    r = await client.get(
        "/api/v1/ohlcv/BTCUSDT",
        params={"start": "2026-08-19T00:00:00Z", "end": "2026-08-18T00:00:00Z"},
        headers=KEY,
    )
    assert r.status_code == 422


async def test_ohlcv_returns_candles_and_preserves_precision(client):
    r = await client.get("/api/v1/ohlcv/BTCUSDT", params={"hours": 6, "limit": 5}, headers=KEY)
    assert r.status_code == 200
    rows = r.json()
    if rows:
        # Serialised as strings, not floats -- precision must survive the wire.
        assert isinstance(rows[0]["open"], str)
        assert rows[0]["symbol"] == "BTCUSDT"


async def test_cursor_pagination_advances_without_overlap(client):
    first = await client.get(
        "/api/v1/prices/BTCUSDT", params={"hours": 24, "limit": 5}, headers=KEY
    )
    assert first.status_code == 200
    page1 = first.json()
    if not page1["has_more"]:
        pytest.skip("not enough tick history to paginate yet")

    second = await client.get(
        "/api/v1/prices/BTCUSDT",
        params={"hours": 24, "limit": 5, "cursor": page1["next_cursor"]},
        headers=KEY,
    )
    assert second.status_code == 200
    page2 = second.json()

    ids1 = {i["trade_id"] for i in page1["items"]}
    ids2 = {i["trade_id"] for i in page2["items"]}
    assert not (ids1 & ids2), "pages overlapped -- keyset pagination is broken"


async def test_malformed_cursor_is_a_client_error(client):
    r = await client.get(
        "/api/v1/prices/BTCUSDT",
        params={"hours": 1, "cursor": "!!!not-base64!!!"},
        headers=KEY,
    )
    assert r.status_code == 422


async def test_cache_hit_is_reported_on_second_call(client):
    a = await client.get("/api/v1/prices/latest/BTCUSDT", headers=KEY)
    if a.status_code == 404:
        pytest.skip("no ticks recorded yet")
    b = await client.get("/api/v1/prices/latest/BTCUSDT", headers=KEY)
    assert b.status_code == 200
    assert b.json()["source"] == "cache"


async def test_ask_is_disabled_without_a_configured_key(client):
    from app.core.config import settings

    r = await client.post("/api/v1/ask", json={"question": "what is BTC?"}, headers=KEY)
    if settings.anthropic_api_key:
        assert r.status_code in (200, 429)
    else:
        assert r.status_code == 503


async def test_tool_surface_is_published(client):
    r = await client.get("/api/v1/ask/tools", headers=KEY)
    assert r.status_code == 200
    body = r.json()
    assert {t["name"] for t in body["tools"]} >= {"get_latest_price", "compare_symbols"}
    assert body["guardrails"]["sql_generation"].startswith("disabled")


async def test_market_summary_ranks_symbols(client):
    """Regression: the volatility expression mixed NUMERIC with the double
    precision returned by SQRT(), and ROUND(double, int) does not exist."""
    r = await client.get("/api/v1/market/summary", params={"hours": 6}, headers=KEY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "window" in body
    for row in body["symbols"]:
        assert {"symbol", "pct_change", "daily_vol_pct", "volume"} <= set(row)


async def test_market_summary_respects_the_window_cap(client):
    r = await client.get(
        "/api/v1/market/summary",
        params={"start": "1970-01-01T00:00:00Z", "end": "2030-01-01T00:00:00Z"},
        headers=KEY,
    )
    assert r.status_code == 422
