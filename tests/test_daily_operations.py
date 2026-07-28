from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from danta.adapters.krx.client import MarketDataset
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
    monkeypatch.setattr(close_prefetch, "load_krx_environment", lambda _settings: None)
    monkeypatch.setattr(
        close_prefetch.PykrxMarketDataClient,
        "collect",
        lambda _self, **_kwargs: MarketDataset(
            bars={},
            names={},
            flows={},
            trading_dates=[datetime(2026, 7, 27).date()],
            market_caps={},
        ),
    )
    result = await run_close_prefetch(
        AppSettings(daily_run_root=tmp_path),
        now=datetime(2026, 7, 28, 15, 31, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    assert result.status == "NON_TRADING_DAY"


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
