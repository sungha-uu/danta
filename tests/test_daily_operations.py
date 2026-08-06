from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from danta.adapters.kis.client import KisDailyBar
from danta.config import AppSettings
from danta.services import close_prefetch, daily_operations
from danta.services.close_prefetch import run_close_prefetch
from danta.services.daily_operations import (
    DailyOperationError,
    _exclusive_lock,
    run_scheduled_refresh,
)
from danta.services.daily_pipeline import DailyPipelineResult


@pytest.mark.asyncio
async def test_scheduled_refresh_skips_weekend_without_calling_providers(
    tmp_path: Path,
) -> None:
    settings = AppSettings(
        daily_run_root=tmp_path,
        smtp_enabled=False,
        daily_publish_enabled=False,
        daily_notify_enabled=False,
    )
    result = await run_scheduled_refresh(
        settings,
        now=datetime(2026, 8, 1, 16, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    assert result.status == "NON_TRADING_DAY"


@pytest.mark.asyncio
async def test_close_prefetch_skips_weekend_without_calling_providers(
    tmp_path: Path,
) -> None:
    settings = AppSettings(daily_run_root=tmp_path)
    result = await run_close_prefetch(
        settings,
        now=datetime(2026, 8, 1, 15, 31, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    assert result.status == "NON_TRADING_DAY"


@pytest.mark.asyncio
async def test_close_prefetch_skips_weekday_market_holiday(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeKisClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeKisClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def daily_bars(self, *_args: object, **_kwargs: object) -> list[KisDailyBar]:
            return [KisDailyBar("20260727", 100, 1, 100)]

    monkeypatch.setattr(close_prefetch, "KisClient", FakeKisClient)
    result = await run_close_prefetch(
        AppSettings(
            daily_run_root=tmp_path,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'holiday.db'}",
        ),
        now=datetime(2026, 7, 28, 15, 31, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    assert result.status == "NON_TRADING_DAY"


@pytest.mark.asyncio
async def test_close_prefetch_publishes_final_overlay_without_krx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeKisClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeKisClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def daily_bars(self, *_args: object, **_kwargs: object) -> list[KisDailyBar]:
            return [KisDailyBar("20260728", 100, 1, 100)]

    observed: list[datetime] = []

    class FakeOverlay:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def refresh(self, now: datetime) -> dict[str, object]:
            observed.append(now)
            return {"coverage": 200}

    monkeypatch.setattr(close_prefetch, "KisClient", FakeKisClient)
    monkeypatch.setattr(close_prefetch, "IntradayCandidateOverlay", FakeOverlay)
    settings = AppSettings(
        daily_run_root=tmp_path / "runs",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'close.db'}",
    )
    result = await run_close_prefetch(
        settings,
        now=datetime(2026, 7, 28, 15, 31, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    assert result.status == "COMPLETED"
    assert result.symbol_count == 200
    assert observed and observed[0].tzinfo is not None
    assert (settings.daily_run_root / "2026-07-28-prefetch.json").exists()


def test_daily_refresh_lock_rejects_overlapping_run(tmp_path: Path) -> None:
    lock = tmp_path / "daily.lock"
    with (
        _exclusive_lock(lock),
        pytest.raises(DailyOperationError, match="already running"),
        _exclusive_lock(lock),
    ):
        pass
    assert not lock.exists()


@pytest.mark.asyncio
async def test_success_marker_makes_retry_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = datetime(2026, 7, 28, 16, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    calls = 0

    async def fake_pipeline(*_args: object, **_kwargs: object) -> DailyPipelineResult:
        nonlocal calls
        calls += 1
        return DailyPipelineResult(
            report_path=tmp_path / "report.json",
            dashboard_path=tmp_path / "dist" / "index.html",
            candidate_count=12,
            deep_review_count=12,
            data_as_of=current,
            fundamental_snapshot_count=198,
            fundamental_unavailable_count=2,
        )

    monkeypatch.setattr(daily_operations, "run_daily_pipeline", fake_pipeline)
    settings = AppSettings(
        daily_run_root=tmp_path / "runs",
        smtp_enabled=False,
        daily_publish_enabled=False,
        daily_notify_enabled=False,
    )

    first = await run_scheduled_refresh(
        settings,
        now=current,
        publish=False,
        notify=False,
    )
    second = await run_scheduled_refresh(
        settings,
        now=current,
        publish=False,
        notify=False,
    )

    assert first.status == "COMPLETED"
    assert second.status == "ALREADY_COMPLETED"
    assert calls == 1
    assert (settings.daily_run_root / "2026-07-28-success.json").exists()
