from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from danta.config import AppSettings, KisCredentials
from danta.domain.market import MarketRisk
from danta.domain.trading_session import OrchestratorState
from danta.ports.broker import Quote
from danta.services.command_store import CommandStatus, FileCommandStore
from danta.services.paper_autonomous_campaign import (
    PaperAutonomousCampaignController,
    create_campaign_authorization,
    load_campaign_authorization,
    write_campaign_authorization,
)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.prices: list[tuple[str, int]] = []
        self.selections: list[tuple[str, str, int, object]] = []
        self.calls: list[str] = []

    def send_autonomous_selection_completed(
        self,
        selections: list[tuple[str, str, int, object]],
    ) -> SimpleNamespace:
        self.selections = selections
        self.calls.append("selection")
        return SimpleNamespace(recipient_count=1)

    def send_entry_prices_determined(
        self,
        prices: list[tuple[str, int]],
    ) -> SimpleNamespace:
        self.prices = prices
        self.calls.append("prices")
        return SimpleNamespace(recipient_count=1)


class _LiveQuoteBroker:
    async def current_price(self, symbol: str) -> Quote:
        prices = {
            "475150": 43_100,
            "010060": 169_000,
            "071970": 47_500,
        }
        return Quote(
            symbol=symbol,
            price=prices.get(symbol, 100_000),
            change_rate=None,
            raw_timestamp=None,
        )


def _credentials(environment: str = "paper") -> KisCredentials:
    return KisCredentials(
        environment=environment,
        app_key="key",
        app_secret="secret",
        account_no="12345678",
        product_code="01",
        hts_id="tester",
    )


def test_campaign_authorization_is_limited_to_90_days() -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    authorization = create_campaign_authorization(now=now, days=90)
    assert authorization.expires_at - authorization.approved_at == timedelta(days=90)
    with pytest.raises(ValueError, match="between 1 and 90"):
        create_campaign_authorization(now=now, days=91)


def test_paper_authorization_is_rejected_by_prod_even_if_file_exists(
    tmp_path: Path,
) -> None:
    path = tmp_path / "campaign.json"
    write_campaign_authorization(
        create_campaign_authorization(
            now=datetime(2026, 7, 31, tzinfo=UTC),
            days=30,
        ),
        path,
    )
    settings = AppSettings(
        environment="prod",
        kis_credentials_path=tmp_path / "prod.json",
        paper_autonomous_campaign_path=path,
    )
    with pytest.raises(PermissionError, match="forbidden outside paper"):
        load_campaign_authorization(settings, _credentials("prod"))


@pytest.mark.asyncio
async def test_controller_submits_one_agent_reviewed_batch_per_report(
    tmp_path: Path,
) -> None:
    report = tmp_path / "reviewed.json"
    shutil.copy(Path("data/candidate_intraday_ai_report.json"), report)
    authorization_path = tmp_path / "campaign.json"
    now = datetime(2026, 7, 30, 1, 0, tzinfo=UTC)  # 10:00 KST
    authorization = create_campaign_authorization(now=now - timedelta(minutes=1))
    write_campaign_authorization(authorization, authorization_path)
    settings = AppSettings(
        paper_autonomous_campaign_path=authorization_path,
        paper_autonomous_kill_switch_path=tmp_path / "STOP",
        paper_autonomous_report_path=report,
    )
    core = SimpleNamespace(
        positions={},
        submitted={},
        market_risk=MarketRisk.NORMAL,
        market_entry_resume_required=False,
        orchestrator=SimpleNamespace(state=OrchestratorState.RUNNING),
    )
    store = FileCommandStore(tmp_path / "commands")
    repository = SimpleNamespace(audit=AsyncMock())
    notifier = _RecordingNotifier()
    controller = PaperAutonomousCampaignController(
        settings=settings,
        credentials=_credentials(),
        command_store=store,
        core=core,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
        broker=_LiveQuoteBroker(),  # type: ignore[arg-type]
    )

    mandate = await controller.tick(now)

    assert mandate is not None
    assert 1 <= len(mandate.selections) <= 3
    assert all(item.ai_grade in {"STRONG_RECOMMEND", "RECOMMEND"} for item in mandate.selections)
    assert [item.rank for item in mandate.selections] == sorted(
        item.rank for item in mandate.selections
    )
    assert all(
        item.entry_price_source == "PAPER_AUTONOMOUS_REPORT_PRICE" for item in mandate.selections
    )
    assert notifier.prices == [
        (item.name, item.entry_target_price_krw) for item in mandate.selections
    ]
    assert [item[0] for item in notifier.selections] == [
        item.name for item in mandate.selections
    ]
    assert notifier.calls == ["selection", "prices"]
    assert store.accept_next() is not None

    # Moving the accepted command aside simulates a completed runtime command;
    # durable campaign state must still prevent the same report being submitted.
    store.archive_active(
        mandate.command_id,
        status=CommandStatus.COMPLETED,
        reason="TEST",
    )
    assert await controller.tick(now + timedelta(minutes=1)) is None


@pytest.mark.asyncio
async def test_kill_switch_blocks_only_new_campaign_mandate(
    tmp_path: Path,
) -> None:
    report = tmp_path / "reviewed.json"
    shutil.copy(Path("data/candidate_intraday_ai_report.json"), report)
    authorization_path = tmp_path / "campaign.json"
    now = datetime(2026, 7, 31, 1, 0, tzinfo=UTC)
    write_campaign_authorization(
        create_campaign_authorization(now=now - timedelta(minutes=1)),
        authorization_path,
    )
    stop = tmp_path / "STOP"
    stop.write_text("stop", encoding="utf-8")
    settings = AppSettings(
        paper_autonomous_campaign_path=authorization_path,
        paper_autonomous_kill_switch_path=stop,
        paper_autonomous_report_path=report,
    )
    controller = PaperAutonomousCampaignController(
        settings=settings,
        credentials=_credentials(),
        command_store=FileCommandStore(tmp_path / "commands"),
        core=SimpleNamespace(  # type: ignore[arg-type]
            positions={},
            submitted={},
            market_risk=MarketRisk.NORMAL,
            market_entry_resume_required=False,
            orchestrator=SimpleNamespace(state=OrchestratorState.RUNNING),
        ),
        repository=SimpleNamespace(audit=AsyncMock()),  # type: ignore[arg-type]
    )
    assert await controller.tick(now) is None
