from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from danta.adapters.kis.realtime import (
    ExpectedPriceTick,
    MarketVenue,
    TradeTick,
)
from danta.domain.market import MarketRisk
from danta.domain.premarket import PremarketAction, PremarketPolicy
from danta.domain.risk import ExitUrgency
from danta.domain.trading_session import IntentSide, SymbolSession, SymbolState
from danta.services.capital_allocator import CapitalAllocator
from danta.services.overnight_guardian import (
    OvernightPosition,
    OvernightProtectionCoordinator,
    release_opening_plans_to_orchestrator,
)
from danta.services.priority_intent_scheduler import PriorityIntentScheduler
from danta.services.trading_orchestrator import TradingOrchestrator

KST = ZoneInfo("Asia/Seoul")


def _policy() -> PremarketPolicy:
    return PremarketPolicy(
        version="premarket-test-v1",
        approved=True,
        minimum_nxt_trade_samples=3,
        maximum_snapshot_age_seconds=30,
        early_loss_pct=Decimal("-3"),
        strong_loss_pct=Decimal("-5"),
        sell_pressure_threshold=Decimal("0.7"),
        market_stress_threshold=Decimal("0.75"),
    )


def _trade(
    observed_at: datetime,
    *,
    price: int,
    sell_pressure: bool = True,
    venue: MarketVenue = MarketVenue.NXT,
) -> TradeTick:
    return TradeTick(
        symbol="000660",
        observed_at=observed_at,
        price=price,
        best_ask=price + 1000,
        best_bid=price,
        trade_volume=10,
        accumulated_value=1_000_000,
        sell_trade_count=90 if sell_pressure else 30,
        buy_trade_count=10 if sell_pressure else 70,
        trade_strength=Decimal("30") if sell_pressure else Decimal("120"),
        ask_quantity=1000 if sell_pressure else 100,
        bid_quantity=100 if sell_pressure else 1000,
        total_ask_quantity=5000 if sell_pressure else 1000,
        total_bid_quantity=1000 if sell_pressure else 5000,
        venue=venue,
    )


def _expected(observed_at: datetime, price: int) -> ExpectedPriceTick:
    return ExpectedPriceTick(
        symbol="000660",
        observed_at=observed_at,
        expected_price=price,
        best_ask=price + 1000,
        best_bid=price,
        expected_volume=1000,
        change_rate=None,
        venue=MarketVenue.KRX,
    )


def _coordinator() -> OvernightProtectionCoordinator:
    return OvernightProtectionCoordinator(
        [
            OvernightPosition(
                symbol="000660",
                generation=2,
                average_entry_price=Decimal("1800000"),
                sellable_quantity=4,
            )
        ],
        policy=_policy(),
    )


def test_nxt_alone_cannot_trigger_early_exit_or_buy() -> None:
    coordinator = _coordinator()
    now = datetime(2026, 7, 28, 8, 50, tzinfo=KST)
    for second in range(3):
        coordinator.process_event(
            _trade(now.replace(second=second), price=1_710_000)
        )
    plans = coordinator.lock_opening_plans(
        now=now.replace(second=5),
        market_risk=MarketRisk.NORMAL,
        market_stress_score=Decimal("0.2"),
    )
    assert plans[0].decision.action is PremarketAction.HOLD
    assert plans[0].decision.reason_codes == ("KRX_EXPECTED_OPEN_REQUIRED",)


def test_confirmed_strong_loss_locks_full_exit_for_krx_open() -> None:
    coordinator = _coordinator()
    now = datetime(2026, 7, 28, 8, 50, tzinfo=KST)
    for second in range(3):
        coordinator.process_event(
            _trade(now.replace(second=second), price=1_700_000)
        )
    coordinator.process_event(_expected(now.replace(second=4), 1_700_000))
    plans = coordinator.lock_opening_plans(
        now=now.replace(second=5),
        market_risk=MarketRisk.NORMAL,
        market_stress_score=Decimal("0.8"),
    )
    assert plans[0].decision.action is PremarketAction.PLAN_FULL_EXIT
    assert plans[0].decision.urgency is ExitUrgency.PROTECTIVE
    assert coordinator.release_exit_decisions(
        now=datetime(2026, 7, 28, 8, 59, tzinfo=KST)
    ) == ()
    released = coordinator.release_exit_decisions(
        now=datetime(2026, 7, 28, 9, 0, tzinfo=KST)
    )
    assert released[0].quantity == 4
    assert released[0].reason_codes == ("PREMARKET_STRONG_DEFENSE",)


def test_minus_seven_remains_hard_stop_and_expired_plan_fails_closed() -> None:
    coordinator = _coordinator()
    now = datetime(2026, 7, 28, 8, 50, tzinfo=KST)
    for second in range(3):
        coordinator.process_event(
            _trade(now.replace(second=second), price=1_670_000)
        )
    plans = coordinator.lock_opening_plans(
        now=now.replace(second=5),
        market_risk=MarketRisk.NORMAL,
        market_stress_score=Decimal("0.1"),
    )
    assert plans[0].decision.urgency is ExitUrgency.HARD_STOP
    with pytest.raises(RuntimeError, match="expired"):
        coordinator.release_exit_decisions(
            now=datetime(2026, 7, 28, 9, 6, tzinfo=KST)
        )


def test_krx_events_do_not_pollute_nxt_signal() -> None:
    coordinator = _coordinator()
    now = datetime(2026, 7, 28, 8, 50, tzinfo=KST)
    krx_trade = _trade(now, price=1_700_000, venue=MarketVenue.KRX)
    coordinator.process_event(krx_trade)
    with pytest.raises(ValueError, match="NXT trade data"):
        coordinator.guardians["000660"].evaluate(
            policy=_policy(),
            now=now,
            market_risk=MarketRisk.NORMAL,
            market_stress_score=Decimal("0"),
        )


@pytest.mark.asyncio
async def test_opening_plan_enters_existing_sell_only_priority_queue() -> None:
    coordinator = _coordinator()
    now = datetime(2026, 7, 28, 8, 50, tzinfo=KST)
    for second in range(3):
        coordinator.process_event(_trade(now.replace(second=second), price=1_670_000))
    coordinator.lock_opening_plans(
        now=now.replace(second=5),
        market_risk=MarketRisk.NORMAL,
        market_stress_score=Decimal("0.1"),
    )
    scheduler = PriorityIntentScheduler()
    orchestrator = TradingOrchestrator(
        capital_allocator=CapitalAllocator(),
        scheduler=scheduler,
    )
    orchestrator.sessions["000660"] = SymbolSession(
        symbol="000660",
        generation=2,
        state=SymbolState.POSITION_OPEN,
        quantity=4,
        sellable_quantity=4,
    )
    intents = await release_opening_plans_to_orchestrator(
        coordinator,
        orchestrator,
        now=datetime(2026, 7, 28, 9, 0, tzinfo=KST),
    )
    assert len(intents) == 1
    assert intents[0].side is IntentSide.SELL
    assert intents[0].order_type == "MARKET"
