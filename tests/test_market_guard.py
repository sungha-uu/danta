from decimal import Decimal

from danta.domain.market import MarketRisk
from danta.domain.market_wide import MarketWideRiskLevel
from danta.services.market_guard import (
    MarketGuardObservation,
    MarketRegimeGuard,
)


def test_risk_off_requires_repeated_market_wide_collapse() -> None:
    guard = MarketRegimeGuard()
    observation = MarketGuardObservation(Decimal("-3.0"), Decimal("0.9"))
    assert guard.observe(observation).risk is MarketRisk.CAUTION
    assert guard.observe(observation).risk is MarketRisk.CAUTION
    assert guard.observe(observation).risk is MarketRisk.RISK_OFF


def test_normal_market_does_not_block_entries() -> None:
    decision = MarketRegimeGuard().observe(
        MarketGuardObservation(Decimal("0.2"), Decimal("0.4"))
    )
    assert decision.risk is MarketRisk.NORMAL


def test_panic_market_blocks_entries_without_confirmation_delay() -> None:
    decision = MarketRegimeGuard().observe(
        MarketGuardObservation(
            Decimal("-8.0"),
            Decimal("0.92"),
            market_emergency=True,
        )
    )
    assert decision.risk is MarketRisk.RISK_OFF
    assert decision.level is MarketWideRiskLevel.PANIC


def test_foreign_outflow_pension_buying_is_proxy_not_standalone_alarm() -> None:
    decision = MarketRegimeGuard().observe(
        MarketGuardObservation(
            Decimal("-0.5"),
            Decimal("0.45"),
            foreign_net_ratio=Decimal("-0.005"),
            pension_net_million=100_000,
        )
    )
    assert decision.risk is MarketRisk.NORMAL
    assert "FOREIGN_OUTFLOW_PENSION_ABSORPTION_PROXY" in decision.reason_codes


def test_incomplete_market_feed_fails_closed_for_new_entries() -> None:
    decision = MarketRegimeGuard().observe(
        MarketGuardObservation(
            Decimal("0"),
            Decimal("0"),
            provider_complete=False,
        )
    )
    assert decision.risk is MarketRisk.RISK_OFF
    assert decision.level is MarketWideRiskLevel.RISK_OFF
