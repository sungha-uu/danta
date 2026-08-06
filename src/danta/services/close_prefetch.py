from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from danta.adapters.kis.client import KisClient
from danta.config import (
    AppSettings,
    load_kis_credentials,
)
from danta.db.session import create_engine_and_session
from danta.services.daily_operations import _exclusive_lock
from danta.services.intraday_candidate_overlay import IntradayCandidateOverlay
from danta.services.runtime_repository import SqlRuntimeRepository

KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class ClosePrefetchResult:
    status: Literal["COMPLETED", "ALREADY_COMPLETED", "NON_TRADING_DAY"]
    trade_date: str
    completed_at: str
    symbol_count: int


async def run_close_prefetch(
    settings: AppSettings,
    *,
    now: datetime | None = None,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ClosePrefetchResult:
    current = (now or datetime.now(KST)).astimezone(KST)
    trade_date = current.date().isoformat()
    emit = progress if progress is not None else lambda _message: None
    if current.weekday() >= 5 and not force:
        return ClosePrefetchResult(
            status="NON_TRADING_DAY",
            trade_date=trade_date,
            completed_at=current.isoformat(),
            symbol_count=0,
        )
    settings.daily_run_root.mkdir(parents=True, exist_ok=True)
    marker = settings.daily_run_root / f"{trade_date}-prefetch.json"
    if marker.exists() and not force:
        return ClosePrefetchResult(
            status="ALREADY_COMPLETED",
            trade_date=trade_date,
            completed_at=current.isoformat(),
            symbol_count=0,
        )
    with _exclusive_lock(settings.daily_run_root / "close-prefetch.lock"):
        credentials = load_kis_credentials(settings)
        engine, session_factory = create_engine_and_session(settings.database_url)
        try:
            async with KisClient(
                credentials,
                token_cache_path=settings.kis_token_cache_path,
            ) as client:
                reference = await client.daily_bars(
                    "005930",
                    start_date=current.strftime("%Y%m%d"),
                    end_date=current.strftime("%Y%m%d"),
                )
                if not any(item.trading_date == current.strftime("%Y%m%d") for item in reference):
                    return ClosePrefetchResult(
                        status="NON_TRADING_DAY",
                        trade_date=trade_date,
                        completed_at=datetime.now(KST).isoformat(),
                        symbol_count=0,
                    )
                emit("collecting and publishing final 15:30 KIS intraday overlay")
                payload = await IntradayCandidateOverlay(
                    settings=settings,
                    broker=client,
                    repository=SqlRuntimeRepository(session_factory),
                ).refresh(current.astimezone(UTC))
        finally:
            await engine.dispose()
        coverage = payload.get("coverage")
        if not isinstance(coverage, int):
            raise RuntimeError("final intraday overlay did not report integer coverage")
        symbol_count = coverage
        result = ClosePrefetchResult(
            status="COMPLETED",
            trade_date=trade_date,
            completed_at=datetime.now(KST).isoformat(),
            symbol_count=symbol_count,
        )
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(marker)
        return result
