from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from danta.adapters.kis.realtime import (
    ExpectedPriceTick,
    MarketVenue,
    OrderBookTick,
    RealtimeEvent,
    TradeTick,
)
from danta.domain.market import MarketRisk
from danta.domain.premarket import (
    PremarketDecision,
    PremarketPolicy,
    PremarketSnapshot,
    evaluate_premarket,
)
from danta.domain.risk import ExitAction, ExitDecision
from danta.domain.trading_session import OrderIntent
from danta.services.market_signal import RollingMarketSignal
from danta.services.trading_orchestrator import TradingOrchestrator

KST = ZoneInfo("Asia/Seoul")
NXT_WATCH_START = time(8, 0)
OPENING_PLAN_LOCK = time(8, 50)
KRX_OPEN = time(9, 0)
OPENING_PLAN_EXPIRY = time(9, 5)


@dataclass(frozen=True, slots=True)
class OvernightPosition:
    symbol: str
    generation: int
    average_entry_price: Decimal
    sellable_quantity: int

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        if self.generation < 0:
            raise ValueError("generation must not be negative")
        if self.average_entry_price <= 0:
            raise ValueError("average_entry_price must be positive")
        if self.sellable_quantity < 0:
            raise ValueError("sellable_quantity must not be negative")


@dataclass(frozen=True, slots=True)
class OpeningExitPlan:
    decision: PremarketDecision
    locked_at: datetime
    trading_date: str


class SymbolOvernightGuardian:
    def __init__(self, position: OvernightPosition) -> None:
        self.position = position
        self.signal = RollingMarketSignal(position.symbol)
        self.latest_nxt_trade: TradeTick | None = None
        self.latest_krx_expected: ExpectedPriceTick | None = None

    def update(self, event: RealtimeEvent) -> None:
        if event.symbol != self.position.symbol:
            raise ValueError("event symbol does not match overnight position")
        if isinstance(event, ExpectedPriceTick):
            if event.venue is MarketVenue.KRX:
                self.latest_krx_expected = event
            return
        if event.venue is not MarketVenue.NXT:
            return
        if isinstance(event, TradeTick):
            self.latest_nxt_trade = event
        if isinstance(event, (TradeTick, OrderBookTick)):
            self.signal.update(event)

    def evaluate(
        self,
        *,
        policy: PremarketPolicy,
        now: datetime,
        market_risk: MarketRisk,
        market_stress_score: Decimal,
    ) -> PremarketDecision:
        latest = self.latest_nxt_trade
        if latest is None:
            raise ValueError("NXT trade data is not available")
        signal = self.signal.snapshot(
            now=now,
            market_risk=market_risk,
            market_stress_score=market_stress_score,
            box_valid=True,
            data_fresh=True,
        )
        expected = self.latest_krx_expected
        observed_at = max(
            latest.observed_at,
            expected.observed_at if expected is not None else latest.observed_at,
        )
        return evaluate_premarket(
            PremarketSnapshot(
                symbol=self.position.symbol,
                generation=self.position.generation,
                average_entry_price=self.position.average_entry_price,
                sellable_quantity=self.position.sellable_quantity,
                nxt_price=latest.price,
                nxt_trade_samples=self.signal.trade_count,
                nxt_sell_pressure=signal.sell_pressure_score,
                krx_expected_open_price=(
                    expected.expected_price if expected is not None else None
                ),
                market_stress_score=market_stress_score,
                market_risk=market_risk,
                observed_at=observed_at,
                data_fresh=True,
            ),
            policy=policy,
            now=now,
        )


class OvernightProtectionCoordinator:
    """Coordinates many held symbols without granting any buy authority."""

    def __init__(
        self,
        positions: list[OvernightPosition],
        *,
        policy: PremarketPolicy,
    ) -> None:
        if not positions:
            raise ValueError("at least one overnight position is required")
        if len({item.symbol for item in positions}) != len(positions):
            raise ValueError("overnight positions must have unique symbols")
        self.policy = policy
        self.guardians = {
            item.symbol: SymbolOvernightGuardian(item) for item in positions
        }
        self.plans: dict[str, OpeningExitPlan] = {}

    def process_event(self, event: RealtimeEvent) -> None:
        guardian = self.guardians.get(event.symbol)
        if guardian is not None:
            guardian.update(event)

    def lock_opening_plans(
        self,
        *,
        now: datetime,
        market_risk: MarketRisk,
        market_stress_score: Decimal,
    ) -> tuple[OpeningExitPlan, ...]:
        local = _kst(now)
        if local.time() < OPENING_PLAN_LOCK or local.time() >= KRX_OPEN:
            raise ValueError("opening plans may only be locked from 08:50 to 08:59 KST")
        plans: list[OpeningExitPlan] = []
        for symbol, guardian in self.guardians.items():
            try:
                decision = guardian.evaluate(
                    policy=self.policy,
                    now=now,
                    market_risk=market_risk,
                    market_stress_score=market_stress_score,
                )
            except ValueError:
                continue
            plan = OpeningExitPlan(
                decision=decision,
                locked_at=now,
                trading_date=local.date().isoformat(),
            )
            self.plans[symbol] = plan
            plans.append(plan)
        return tuple(plans)

    def release_exit_decisions(self, *, now: datetime) -> tuple[ExitDecision, ...]:
        local = _kst(now)
        if local.time() < KRX_OPEN:
            return ()
        if local.time() > OPENING_PLAN_EXPIRY:
            raise RuntimeError(
                "opening plan expired; reconcile positions and reassess before ordering"
            )
        trading_date = local.date().isoformat()
        decisions: list[ExitDecision] = []
        for plan in self.plans.values():
            if plan.trading_date != trading_date:
                continue
            decision = plan.decision.to_exit_decision()
            if decision.action is ExitAction.SELL_MARKET:
                decisions.append(decision)
        return tuple(decisions)


def _kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(KST)


async def release_opening_plans_to_orchestrator(
    coordinator: OvernightProtectionCoordinator,
    orchestrator: TradingOrchestrator,
    *,
    now: datetime,
) -> tuple[OrderIntent, ...]:
    """Release sell-only decisions through the existing idempotent priority queue."""
    intents: list[OrderIntent] = []
    for decision in coordinator.release_exit_decisions(now=now):
        intent = await orchestrator.handle_exit_decision(decision, created_at=now)
        if intent is not None:
            intents.append(intent)
    return tuple(intents)
