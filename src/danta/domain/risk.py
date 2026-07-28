from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from danta.domain.market import MarketRisk

STOP_LOSS_RATE = Decimal("0.07")


def weighted_average_fill(fills: list[tuple[int, int]]) -> Decimal:
    total_quantity = sum(quantity for _, quantity in fills)
    if total_quantity <= 0:
        raise ValueError("total fill quantity must be positive")
    total_value = sum(
        (Decimal(price) * quantity for price, quantity in fills),
        start=Decimal("0"),
    )
    return total_value / total_quantity


def hard_stop_price(average_entry_price: Decimal, tick_size: int = 1) -> int:
    if average_entry_price <= 0:
        raise ValueError("average entry price must be positive")
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    raw = average_entry_price * (Decimal("1") - STOP_LOSS_RATE)
    ticks = (raw / Decimal(tick_size)).to_integral_value(rounding=ROUND_FLOOR)
    return int(ticks * tick_size)


@dataclass(frozen=True, slots=True)
class RiskObservation:
    last_price: int
    best_bid: int | None = None
    broker_return_pct: Decimal | None = None


def hard_stop_triggered(stop_price: int, observation: RiskObservation) -> bool:
    price_triggered = observation.last_price <= stop_price
    bid_triggered = observation.best_bid is not None and observation.best_bid <= stop_price
    return_triggered = (
        observation.broker_return_pct is not None
        and observation.broker_return_pct <= Decimal("-7.0")
    )
    return price_triggered or bid_triggered or return_triggered


class ExitAction(StrEnum):
    HOLD = "HOLD"
    SELL_MARKET = "SELL_MARKET"


class ExitUrgency(StrEnum):
    NONE = "NONE"
    NORMAL = "NORMAL"
    PROTECTIVE = "PROTECTIVE"
    HARD_STOP = "HARD_STOP"


@dataclass(frozen=True, slots=True)
class ExitPolicy:
    version: str
    approved: bool
    early_loss_pct: Decimal
    strong_loss_pct: Decimal
    early_defense_score: Decimal
    strong_sell_pressure: Decimal
    panic_market_stress: Decimal
    profit_arm_pct: Decimal
    profit_giveback_pct: Decimal
    profit_weakness_score: Decimal
    max_holding_minutes: int

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("exit policy version is required")
        if not (Decimal("-7") < self.strong_loss_pct < self.early_loss_pct < 0):
            raise ValueError("loss thresholds must be ordered between -7 and 0")
        for name in (
            "early_defense_score",
            "strong_sell_pressure",
            "panic_market_stress",
            "profit_weakness_score",
        ):
            value = getattr(self, name)
            if value < 0 or value > 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.profit_arm_pct < 0 or self.profit_giveback_pct <= 0:
            raise ValueError("profit thresholds must be non-negative")
        if self.max_holding_minutes <= 0:
            raise ValueError("max_holding_minutes must be positive")


@dataclass(frozen=True, slots=True)
class PositionRiskSnapshot:
    symbol: str
    generation: int
    average_entry_price: Decimal
    quantity: int
    sellable_quantity: int
    last_price: int
    best_bid: int | None
    broker_return_pct: Decimal | None
    peak_return_pct: Decimal
    held_minutes: int
    sell_pressure_score: Decimal
    weakness_score: Decimal
    market_stress_score: Decimal
    market_risk: MarketRisk
    box_valid: bool
    data_fresh: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.average_entry_price <= 0:
            raise ValueError("average_entry_price must be positive")
        if self.quantity <= 0 or self.sellable_quantity < 0:
            raise ValueError("position quantities are invalid")
        if self.sellable_quantity > self.quantity:
            raise ValueError("sellable_quantity cannot exceed quantity")
        if self.last_price <= 0:
            raise ValueError("last_price must be positive")
        if self.held_minutes < 0:
            raise ValueError("held_minutes must not be negative")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        for name in ("sell_pressure_score", "weakness_score", "market_stress_score"):
            value = getattr(self, name)
            if value < 0 or value > 1:
                raise ValueError(f"{name} must be between 0 and 1")

    @property
    def executable_return_pct(self) -> Decimal:
        executable_price = min(
            self.last_price,
            self.best_bid if self.best_bid is not None else self.last_price,
        )
        calculated = (
            (Decimal(executable_price) - self.average_entry_price)
            / self.average_entry_price
            * Decimal("100")
        )
        if self.broker_return_pct is not None:
            return min(calculated, self.broker_return_pct)
        return calculated


@dataclass(frozen=True, slots=True)
class ExitDecision:
    symbol: str
    generation: int
    action: ExitAction
    urgency: ExitUrgency
    quantity: int
    policy_version: str
    reason_codes: tuple[str, ...]


def evaluate_exit(snapshot: PositionRiskSnapshot, *, policy: ExitPolicy) -> ExitDecision:
    return_pct = snapshot.executable_return_pct
    stop_price = hard_stop_price(snapshot.average_entry_price)
    hard_observation = RiskObservation(
        last_price=snapshot.last_price,
        best_bid=snapshot.best_bid,
        broker_return_pct=snapshot.broker_return_pct,
    )
    if hard_stop_triggered(stop_price, hard_observation):
        return ExitDecision(
            snapshot.symbol,
            snapshot.generation,
            ExitAction.SELL_MARKET,
            ExitUrgency.HARD_STOP,
            snapshot.sellable_quantity,
            "hard-stop-v1",
            ("HARD_STOP_MINUS_7",),
        )
    if snapshot.sellable_quantity <= 0:
        return ExitDecision(
            snapshot.symbol,
            snapshot.generation,
            ExitAction.HOLD,
            ExitUrgency.NONE,
            0,
            policy.version,
            ("NO_SELLABLE_QUANTITY",),
        )
    if not policy.approved:
        return ExitDecision(
            snapshot.symbol,
            snapshot.generation,
            ExitAction.HOLD,
            ExitUrgency.NONE,
            0,
            policy.version,
            ("EXIT_POLICY_NOT_APPROVED",),
        )
    if not snapshot.box_valid:
        return ExitDecision(
            snapshot.symbol,
            snapshot.generation,
            ExitAction.SELL_MARKET,
            ExitUrgency.PROTECTIVE,
            snapshot.sellable_quantity,
            policy.version,
            ("BOX_INVALIDATED",),
        )
    if (
        return_pct <= policy.strong_loss_pct
        and (
            snapshot.sell_pressure_score >= policy.strong_sell_pressure
            or snapshot.market_stress_score >= policy.panic_market_stress
            or snapshot.market_risk is MarketRisk.RISK_OFF
        )
    ):
        return ExitDecision(
            snapshot.symbol,
            snapshot.generation,
            ExitAction.SELL_MARKET,
            ExitUrgency.PROTECTIVE,
            snapshot.sellable_quantity,
            policy.version,
            ("STRONG_DEFENSE",),
        )
    defense_score = (
        snapshot.sell_pressure_score
        + snapshot.weakness_score
        + snapshot.market_stress_score
    ) / Decimal("3")
    if return_pct <= policy.early_loss_pct and defense_score >= policy.early_defense_score:
        return ExitDecision(
            snapshot.symbol,
            snapshot.generation,
            ExitAction.SELL_MARKET,
            ExitUrgency.PROTECTIVE,
            snapshot.sellable_quantity,
            policy.version,
            ("EARLY_DEFENSE",),
        )
    if snapshot.held_minutes >= policy.max_holding_minutes:
        return ExitDecision(
            snapshot.symbol,
            snapshot.generation,
            ExitAction.SELL_MARKET,
            ExitUrgency.NORMAL,
            snapshot.sellable_quantity,
            policy.version,
            ("MAX_HOLDING_TIME",),
        )
    profit_floor = snapshot.peak_return_pct - policy.profit_giveback_pct
    if (
        snapshot.peak_return_pct >= policy.profit_arm_pct
        and return_pct <= profit_floor
        and snapshot.weakness_score >= policy.profit_weakness_score
    ):
        return ExitDecision(
            snapshot.symbol,
            snapshot.generation,
            ExitAction.SELL_MARKET,
            ExitUrgency.NORMAL,
            snapshot.sellable_quantity,
            policy.version,
            ("ADAPTIVE_PROFIT_FLOOR",),
        )
    return ExitDecision(
        snapshot.symbol,
        snapshot.generation,
        ExitAction.HOLD,
        ExitUrgency.NONE,
        0,
        policy.version,
        ("NO_EXIT_CONDITION",),
    )
