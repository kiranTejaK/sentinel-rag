import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app


@pytest.mark.asyncio
async def test_root_endpoint() -> None:
    """Verifies that the root landing endpoint returns basic service information."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "SentinelRAG Gateway"
        assert data["health"] == "/healthz"


@pytest.mark.asyncio
async def test_healthz_endpoint_structure() -> None:
    """Verifies that /healthz returns the expected RFC JSON format and dependency telemetry."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
        # In isolated unit tests without active docker services, status may be 503 (degraded) or 200
        assert response.status_code in [200, 503]
        data = response.json()
        assert "status" in data
        assert "dependencies" in data
        assert "postgresql" in data["dependencies"]
        assert "redis" in data["dependencies"]
        assert "latency_ms" in data["dependencies"]["postgresql"]
        assert "latency_ms" in data["dependencies"]["redis"]
        # Verify custom diagnostic headers
        assert "X-Request-Id" in response.headers
        assert "X-Sentinel-Latency-Ms" in response.headers
