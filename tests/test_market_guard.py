from decimal import Decimal

from danta.domain.market import MarketRisk
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
