from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from danta.adapters.kis.client import KisApiError
from danta.config import load_kis_credentials, load_settings
from danta.services.provider_doctor import KisProviderDoctor

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    settings = load_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.environment.value,
        "real_order_execution": "disabled",
    }


@router.get("/provider/kis/doctor")
async def kis_doctor(
    live: bool = Query(default=False),
    symbol: str = Query(default="005930", pattern=r"^[0-9A-Z]{6}$"),
) -> dict[str, object]:
    try:
        settings = load_settings()
        credentials = load_kis_credentials(settings)
        report = await KisProviderDoctor(settings, credentials).run(live=live, symbol=symbol)
        return report.as_public_dict()
    except (ValueError, KisApiError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
