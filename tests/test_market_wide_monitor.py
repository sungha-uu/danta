from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from danta.domain.market_wide import (
    DailyMarketFlow,
    InvestorNetFlow,
    MarketWideRiskLevel,
    MarketWideSnapshot,
    ProgramNetFlow,
)
from danta.services.market_monitor_application import _within_market_session
from danta.services.market_wide_monitor import (
    MarketStatusPublisher,
    MarketWideMonitor,
    build_flow_quality,
    is_market_risk_email_transition,
)


class FakeCollector:
    def __init__(self, snapshots: list[MarketWideSnapshot]) -> None:
        self.snapshots = snapshots

    async def collect(self) -> MarketWideSnapshot:
        return self.snapshots.pop(0)

    def consume_daily_refresh(self) -> list[object]:
        return []


def test_market_email_gate_allows_all_state_transitions() -> None:
    assert is_market_risk_email_transition(
        MarketWideRiskLevel.NORMAL, MarketWideRiskLevel.CAUTION
    )
    assert is_market_risk_email_transition(
        MarketWideRiskLevel.CAUTION, MarketWideRiskLevel.RISK_OFF
    )
    assert is_market_risk_email_transition(
        MarketWideRiskLevel.RISK_OFF, MarketWideRiskLevel.PANIC
    )
    assert is_market_risk_email_transition(
        MarketWideRiskLevel.CAUTION, MarketWideRiskLevel.NORMAL
    )
    assert is_market_risk_email_transition(
        MarketWideRiskLevel.PANIC, MarketWideRiskLevel.RISK_OFF
    )
    assert not is_market_risk_email_transition(
        MarketWideRiskLevel.NORMAL, MarketWideRiskLevel.NORMAL
    )


class FakeRepository:
    def __init__(self) -> None:
        self.rows: list[tuple[MarketWideSnapshot, object]] = []

    async def save(self, snapshot: MarketWideSnapshot, decision: object) -> None:
        self.rows.append((snapshot, decision))

    async def save_daily_flows(self, _flows: list[object]) -> None:
        return None


def _snapshot(
    at: datetime, *, foreign: int, pension: int, program: int
) -> MarketWideSnapshot:
    return MarketWideSnapshot(
        observed_at=at,
        kospi_index=Decimal("5000"),
        kospi_return_pct=Decimal("-1.0"),
        kospi_open=Decimal("5050"),
        kospi_high=Decimal("5060"),
        kospi_low=Decimal("4990"),
        accumulated_trading_value_million=20_000_000,
        rising_issues=300,
        flat_issues=50,
        declining_issues=550,
        upper_limit_issues=0,
        lower_limit_issues=0,
        investor=InvestorNetFlow(
            personal=0,
            foreign=foreign,
            institution=0,
            financial_investment=0,
            insurance=0,
            investment_trust=0,
            private_fund=0,
            bank=0,
            other_finance=0,
            pension_fund_etc=pension,
            other_corporation=0,
        ),
        program=ProgramNetFlow(0, program, program),
    )


@pytest.mark.asyncio
async def test_monitor_calculates_five_and_fifteen_minute_flow_deltas() -> None:
    now = datetime.now(UTC)
    collector = FakeCollector(
        [
            _snapshot(now - timedelta(minutes=15), foreign=-10, pension=1, program=-2),
            _snapshot(now - timedelta(minutes=5), foreign=-30, pension=4, program=-5),
            _snapshot(now, foreign=-80, pension=14, program=-20),
        ]
    )
    repository = FakeRepository()
    monitor = MarketWideMonitor(  # type: ignore[arg-type]
        collector=collector,
        repository=repository,
    )
    await monitor.poll_once()
    await monitor.poll_once()
    snapshot, _ = await monitor.poll_once()
    assert snapshot.foreign_delta_5m == -50
    assert snapshot.foreign_delta_15m == -70
    assert snapshot.pension_delta_5m == 10
    assert snapshot.program_delta_15m == -18


@pytest.mark.asyncio
async def test_status_publisher_writes_data_only_without_git(tmp_path: Path) -> None:
    repository = tmp_path / "report"
    publisher = MarketStatusPublisher(
        repository_path=repository,
        git_push_enabled=False,
    )
    snapshot = _snapshot(datetime.now(UTC), foreign=-100, pension=20, program=-10)
    monitor = MarketWideMonitor(  # type: ignore[arg-type]
        collector=FakeCollector([snapshot]),
        repository=FakeRepository(),
    )
    snapshot, decision = await monitor.poll_once()
    target = await publisher.publish(snapshot, decision)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "danta-market-status-v1"
    assert payload["investor_net_million"]["foreign"] == -100
    assert "account" not in json.dumps(payload)


def test_flow_quality_separates_financial_investment_only_buying() -> None:
    flow = DailyMarketFlow(
        trading_date="20260729",
        kospi_return_pct=Decimal("-2"),
        personal=-10,
        foreign=-20,
        institution=30,
        financial_investment=50,
        insurance=0,
        investment_trust=-10,
        private_fund=0,
        bank=0,
        other_finance=0,
        pension_fund_etc=-10,
        other_corporation=0,
    )
    features = build_flow_quality([flow])
    assert features is not None
    assert features.financial_investment_only_warning is True
    assert features.latest_price_flow_regime == "PRICE_DOWN_WITH_CORE_SELLING"


def test_market_monitor_session_is_weekday_0850_through_1530() -> None:
    assert _within_market_session(datetime(2026, 7, 29, 8, 50))
    assert _within_market_session(datetime(2026, 7, 29, 15, 30))
    assert not _within_market_session(datetime(2026, 7, 29, 8, 49))
    assert not _within_market_session(datetime(2026, 7, 29, 15, 31))
    assert not _within_market_session(datetime(2026, 8, 1, 10, 0))
