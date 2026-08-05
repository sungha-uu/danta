import httpx
import pytest

from danta.main import create_app


@pytest.mark.asyncio
async def test_health_exposes_safe_environment_only() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app": "danta",
        "environment": "prod",
        "real_order_execution": "enabled",
    }
