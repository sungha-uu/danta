from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from danta.config import AppSettings, KisCredentials
from danta.dashboard.builder import load_dashboard_report
from danta.domain.market import MarketRisk
from danta.domain.trading_session import OrchestratorState
from danta.ports.broker import Quote
from danta.services.command_store import CommandStatus, FileCommandStore
from danta.services.paper_autonomous_campaign import (
    AutonomousCandidatePreference,
    PaperAutonomousCampaignController,
    _autonomous_opportunity_score,
    candidate_preference_path,
    create_campaign_authorization,
    load_campaign_authorization,
    write_campaign_authorization,
    write_candidate_preference,
)


def test_intraday_flow_is_weighted_without_becoming_an_absolute_positive_gate() -> None:
    same_price_positive = _autonomous_opportunity_score(
        live_rank=10,
        discount_from_high_pct=Decimal("20"),
        flow_strength_pct=Decimal("1"),
    )
    same_price_mild_outflow = _autonomous_opportunity_score(
        live_rank=10,
        discount_from_high_pct=Decimal("20"),
        flow_strength_pct=Decimal("-1"),
    )
    strong_price_opportunity_with_mild_outflow = _autonomous_opportunity_score(
        live_rank=1,
        discount_from_high_pct=Decimal("40"),
        flow_strength_pct=Decimal("-1"),
    )
    weak_price_opportunity_with_inflow = _autonomous_opportunity_score(
        live_rank=45,
        discount_from_high_pct=Decimal("5"),
        flow_strength_pct=Decimal("1"),
    )

    assert same_price_positive > same_price_mild_outflow
    assert strong_price_opportunity_with_mild_outflow > weak_price_opportunity_with_inflow


class _RecordingNotifier:
    def __init__(self) -> None:
        self.prices: list[tuple[str, int]] = []
        self.selections: list[tuple[str, int, object]] = []
        self.paused_reasons: list[str] = []
        self.calls: list[str] = []

    def send_autonomous_selection_completed(
        self,
        selections: list[tuple[str, int, object]],
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

    def send_autonomous_entry_paused(
        self,
        *,
        reason: str,
        dashboard_url: str,
    ) -> SimpleNamespace:
        assert dashboard_url.startswith("https://")
        self.paused_reasons.append(reason)
        self.calls.append("paused")
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


class _RiskChangingBroker(_LiveQuoteBroker):
    def __init__(self, core: SimpleNamespace) -> None:
        self.core = core

    async def current_price(self, symbol: str) -> Quote:
        self.core.market_risk = MarketRisk.CAUTION
        return await super().current_price(symbol)


def _write_ready_flow_overlay(
    report_path: Path,
    overlay_path: Path,
    observed_at: datetime,
    *,
    revision: str = "test-flow-revision",
) -> None:
    report = load_dashboard_report(report_path)
    candidates = [*report.candidates, *report.extended_watchlist]
    rows = []
    for candidate in candidates:
        metrics = candidate.windows["14"]
        rows.append(
            {
                "symbol": candidate.code,
                "live_rank_14d": metrics.rank or 200,
                "live_price": int(candidate.current_price),
                "live_position_pct": "25.0",
                "discount_from_window_high_pct": "12.0",
                "intraday_flow_status": "READY",
                "intraday_combined_net_qty": 10_000,
                "intraday_flow_strength_pct": "1.0",
            }
        )
    overlay_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "revision": revision,
                "observed_at": observed_at.isoformat(),
                "report_data_as_of": report.data_as_of.isoformat(),
                "coverage": len(rows),
                "failed_symbols": [],
                "rows": rows,
            }
        ),
        encoding="utf-8",
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
    overlay_path = tmp_path / "overlay.json"
    now = datetime(2026, 7, 30, 1, 0, tzinfo=UTC)  # 10:00 KST
    authorization = create_campaign_authorization(now=now - timedelta(minutes=1))
    write_campaign_authorization(authorization, authorization_path)
    _write_ready_flow_overlay(report, overlay_path, now)
    settings = AppSettings(
        paper_autonomous_campaign_path=authorization_path,
        paper_autonomous_kill_switch_path=tmp_path / "STOP",
        paper_autonomous_report_path=report,
        intraday_overlay_path=overlay_path,
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
    assert all(item.ai_grade is None for item in mandate.selections)
    assert all(
        item.selection_basis == "QUANTITATIVE_OPPORTUNITY"
        for item in mandate.selections
    )
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
async def test_fresh_200_name_overlay_drives_rank_and_allows_later_flat_batch(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "reviewed.json"
    shutil.copy(Path("data/candidate_intraday_ai_report.json"), report_path)
    report = load_dashboard_report(report_path)
    candidates = [*report.candidates, *report.extended_watchlist]
    eligible = [
        candidate
        for candidate in candidates
        if candidate.context_status == "READY"
        and candidate.windows["14"].structure_status == "READY"
        and candidate.windows["14"].rank is not None
    ]
    assert len(candidates) == 200
    assert eligible

    authorization_path = tmp_path / "campaign.json"
    overlay_path = tmp_path / "overlay.json"
    now = datetime(2026, 7, 30, 1, 0, tzinfo=UTC)
    authorization = create_campaign_authorization(now=now - timedelta(minutes=1))
    write_campaign_authorization(authorization, authorization_path)
    rows = [
        {
            "symbol": candidate.code,
            "live_rank_14d": 200,
            "live_price": int(candidate.current_price),
            "live_position_pct": "50.0",
            "discount_from_window_high_pct": "10.0",
            "intraday_flow_status": "READY",
            "intraday_combined_net_qty": 10_000,
            "intraday_flow_strength_pct": "1.0",
        }
        for candidate in candidates
    ]
    chosen = eligible[-1]
    discounted_outflow = eligible[0]
    discounted_row = next(row for row in rows if row["symbol"] == discounted_outflow.code)
    discounted_row.update(
        {
            "live_rank_14d": 1,
            "discount_from_window_high_pct": "40.0",
            "intraday_combined_net_qty": -50_000,
            "intraday_flow_strength_pct": "-5.0",
        }
    )
    chosen_row = next(row for row in rows if row["symbol"] == chosen.code)
    chosen_row.update(
        {
            "live_rank_14d": 1,
            "live_price": int(chosen.current_price) + 100,
            "live_position_pct": "5.0",
        }
    )

    def write_overlay(revision: str, observed_at: datetime) -> None:
        overlay_path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "revision": revision,
                    "observed_at": observed_at.isoformat(),
                    "report_data_as_of": report.data_as_of.isoformat(),
                    "coverage": 200,
                    "failed_symbols": [],
                    "rows": rows,
                }
            ),
            encoding="utf-8",
        )

    write_overlay("revision-1", now)
    settings = AppSettings(
        paper_autonomous_campaign_path=authorization_path,
        paper_autonomous_kill_switch_path=tmp_path / "STOP",
        paper_autonomous_report_path=report_path,
        intraday_overlay_path=overlay_path,
    )
    core = SimpleNamespace(
        positions={}, submitted={}, market_risk=MarketRisk.NORMAL,
        market_guard_initialized=True, market_entry_resume_required=False,
        orchestrator=SimpleNamespace(state=OrchestratorState.RUNNING),
    )
    store = FileCommandStore(tmp_path / "commands")
    controller = PaperAutonomousCampaignController(
        settings=settings,
        credentials=_credentials(),
        command_store=store,
        core=core,  # type: ignore[arg-type]
        repository=SimpleNamespace(audit=AsyncMock()),  # type: ignore[arg-type]
        broker=_LiveQuoteBroker(),  # type: ignore[arg-type]
    )

    first = await controller.tick(now)
    assert first is not None
    assert discounted_outflow.code in {item.symbol for item in first.selections}
    assert first.selections[0].rank == 1
    store.accept_next()
    store.archive_active(first.command_id, status=CommandStatus.COMPLETED, reason="TEST")

    later = now + timedelta(minutes=30)
    preferred = next(
        candidate
        for candidate in eligible
        if candidate.code not in {chosen.code, discounted_outflow.code}
    )
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    report_rows = [
        *report_payload["candidates"],
        *report_payload["extended_watchlist"],
    ]
    preferred_payload = next(row for row in report_rows if row["code"] == preferred.code)
    preferred_payload["windows"]["14"]["ai_grade"] = "NOT_RECOMMEND"
    report_path.write_text(
        json.dumps(report_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    write_candidate_preference(
        AutonomousCandidatePreference(
            trading_date=later.astimezone().date(),
            symbols=(preferred.code,),
            selection_policy="REQUIRE_INCLUDE",
            created_at=later,
        ),
        candidate_preference_path(settings),
    )
    write_overlay("revision-2", later)
    second = await controller.tick(later)
    assert second is not None
    assert second.command_id != first.command_id
    assert second.selections[0].symbol == preferred.code
    assert second.selections[0].ai_grade is None
    assert second.selections[0].selection_basis == "QUANTITATIVE_OPPORTUNITY"


def test_candidate_preference_rejects_duplicate_or_invalid_symbols() -> None:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    with pytest.raises(ValueError, match="unique"):
        AutonomousCandidatePreference(
            trading_date=now.date(),
            symbols=("000660", "000660"),
            created_at=now,
        )
    with pytest.raises(ValueError, match="six digits"):
        AutonomousCandidatePreference(
            trading_date=now.date(),
            symbols=("660",),
            created_at=now,
        )


@pytest.mark.asyncio
async def test_market_caution_allows_selection_but_blocks_submission(
    tmp_path: Path,
) -> None:
    report = tmp_path / "reviewed.json"
    shutil.copy(Path("data/candidate_intraday_ai_report.json"), report)
    authorization_path = tmp_path / "campaign.json"
    overlay_path = tmp_path / "overlay.json"
    now = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)
    write_campaign_authorization(
        create_campaign_authorization(now=now - timedelta(minutes=1)),
        authorization_path,
    )
    settings = AppSettings(
        paper_autonomous_campaign_path=authorization_path,
        paper_autonomous_kill_switch_path=tmp_path / "STOP",
        paper_autonomous_report_path=report,
        intraday_overlay_path=overlay_path,
    )
    _write_ready_flow_overlay(report, overlay_path, now)
    core = SimpleNamespace(
        positions={},
        submitted={},
        market_risk=MarketRisk.CAUTION,
        market_guard_initialized=True,
        market_entry_resume_required=False,
        orchestrator=SimpleNamespace(state=OrchestratorState.RUNNING),
    )
    store = FileCommandStore(tmp_path / "commands")
    controller = PaperAutonomousCampaignController(
        settings=settings,
        credentials=_credentials(),
        command_store=store,
        core=core,  # type: ignore[arg-type]
        repository=SimpleNamespace(audit=AsyncMock()),  # type: ignore[arg-type]
        broker=_LiveQuoteBroker(),  # type: ignore[arg-type]
    )

    assert await controller.tick(now) is None
    assert store.load_active() is None
    snapshot = json.loads(
        (tmp_path / "autonomous_next_selection.json").read_text(encoding="utf-8")
    )
    assert snapshot["execution_status"] == "WATCHING"
    assert snapshot["blocked_reason"] == "MARKET_RISK_NOT_NORMAL"


@pytest.mark.asyncio
async def test_active_position_does_not_stop_next_candidate_selection(
    tmp_path: Path,
) -> None:
    report = tmp_path / "reviewed.json"
    shutil.copy(Path("data/candidate_intraday_ai_report.json"), report)
    authorization_path = tmp_path / "campaign.json"
    overlay_path = tmp_path / "overlay.json"
    now = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)
    write_campaign_authorization(
        create_campaign_authorization(now=now - timedelta(minutes=1)),
        authorization_path,
    )
    _write_ready_flow_overlay(report, overlay_path, now)
    settings = AppSettings(
        paper_autonomous_campaign_path=authorization_path,
        paper_autonomous_kill_switch_path=tmp_path / "STOP",
        paper_autonomous_report_path=report,
        intraday_overlay_path=overlay_path,
    )
    notifier = _RecordingNotifier()
    store = FileCommandStore(tmp_path / "commands")
    controller = PaperAutonomousCampaignController(
        settings=settings,
        credentials=_credentials(),
        command_store=store,
        core=SimpleNamespace(  # type: ignore[arg-type]
            positions={"000660": object()},
            submitted={},
            market_risk=MarketRisk.NORMAL,
            market_guard_initialized=True,
            market_entry_resume_required=False,
            orchestrator=SimpleNamespace(state=OrchestratorState.RUNNING),
        ),
        repository=SimpleNamespace(audit=AsyncMock()),  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
        broker=_LiveQuoteBroker(),  # type: ignore[arg-type]
    )

    assert await controller.tick(now) is None
    assert store.load_active() is None
    snapshot_path = tmp_path / "autonomous_next_selection.json"
    assert snapshot_path.exists()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["execution_status"] == "WATCHING"
    assert snapshot["blocked_reason"] == "ACCOUNT_NOT_FLAT"
    assert snapshot["selections"]
    assert all("ai_grade" not in row for row in snapshot["selections"])
    assert notifier.calls == ["selection", "prices"]

    # A fresh overlay with the same symbols refreshes the watch snapshot but
    # must not repeat selection/price emails.
    later = now + timedelta(minutes=1)
    _write_ready_flow_overlay(report, overlay_path, later, revision="revision-2")
    assert await controller.tick(later) is None
    assert notifier.calls == ["selection", "prices"]


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


@pytest.mark.asyncio
async def test_market_pause_email_is_sent_once_per_trading_day(
    tmp_path: Path,
) -> None:
    authorization_path = tmp_path / "campaign.json"
    report = tmp_path / "reviewed.json"
    shutil.copy(Path("data/candidate_intraday_ai_report.json"), report)
    overlay_path = tmp_path / "overlay.json"
    now = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)  # 10:00 KST
    write_campaign_authorization(
        create_campaign_authorization(now=now - timedelta(minutes=1)),
        authorization_path,
    )
    settings = AppSettings(
        paper_autonomous_campaign_path=authorization_path,
        paper_autonomous_kill_switch_path=tmp_path / "STOP",
        paper_autonomous_report_path=report,
        intraday_overlay_path=overlay_path,
    )
    _write_ready_flow_overlay(report, overlay_path, now)
    notifier = _RecordingNotifier()
    controller = PaperAutonomousCampaignController(
        settings=settings,
        credentials=_credentials(),
        command_store=FileCommandStore(tmp_path / "commands"),
        core=SimpleNamespace(  # type: ignore[arg-type]
            positions={},
            submitted={},
            market_risk=MarketRisk.CAUTION,
            market_entry_resume_required=False,
            orchestrator=SimpleNamespace(state=OrchestratorState.RUNNING),
        ),
        repository=SimpleNamespace(audit=AsyncMock()),  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
        broker=_LiveQuoteBroker(),  # type: ignore[arg-type]
    )

    assert await controller.tick(now) is None
    assert await controller.tick(now + timedelta(minutes=1)) is None

    assert notifier.paused_reasons == ["MARKET_RISK_NOT_NORMAL"]
    assert notifier.calls == ["selection", "prices", "paused"]
