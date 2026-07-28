from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from danta.domain.market import MarketRisk
from danta.domain.risk import ExitAction, ExitDecision, ExitUrgency


class PremarketAction(StrEnum):
    HOLD = "HOLD"
    PLAN_FULL_EXIT = "PLAN_FULL_EXIT"


@dataclass(frozen=True, slots=True)
class PremarketPolicy:
    """Paper-challenger policy for turning NXT observations into KRX opening plans."""

    version: str
    approved: bool
    minimum_nxt_trade_samples: int
    maximum_snapshot_age_seconds: int
    early_loss_pct: Decimal
    strong_loss_pct: Decimal
    sell_pressure_threshold: Decimal
    market_stress_threshold: Decimal

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("premarket policy version is required")
        if self.minimum_nxt_trade_samples <= 0:
            raise ValueError("minimum_nxt_trade_samples must be positive")
        if self.maximum_snapshot_age_seconds <= 0:
            raise ValueError("maximum_snapshot_age_seconds must be positive")
        if not (Decimal("-7") < self.strong_loss_pct < self.early_loss_pct < 0):
            raise ValueError("premarket loss thresholds must be ordered between -7 and 0")
        for name in ("sell_pressure_threshold", "market_stress_threshold"):
            value = getattr(self, name)
            if value < 0 or value > 1:
                raise ValueError(f"{name} must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class PremarketSnapshot:
    symbol: str
    generation: int
    average_entry_price: Decimal
    sellable_quantity: int
    nxt_price: int
    nxt_trade_samples: int
    nxt_sell_pressure: Decimal
    krx_expected_open_price: int | None
    market_stress_score: Decimal
    market_risk: MarketRisk
    observed_at: datetime
    data_fresh: bool

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        if self.generation < 0:
            raise ValueError("generation must not be negative")
        if self.average_entry_price <= 0:
            raise ValueError("average_entry_price must be positive")
        if self.sellable_quantity < 0:
            raise ValueError("sellable_quantity must not be negative")
        if self.nxt_price <= 0:
            raise ValueError("nxt_price must be positive")
        if self.nxt_trade_samples < 0:
            raise ValueError("nxt_trade_samples must not be negative")
        if self.krx_expected_open_price is not None and self.krx_expected_open_price <= 0:
            raise ValueError("krx_expected_open_price must be positive")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        for name in ("nxt_sell_pressure", "market_stress_score"):
            value = getattr(self, name)
            if value < 0 or value > 1:
                raise ValueError(f"{name} must be between 0 and 1")

    def return_pct(self, price: int) -> Decimal:
        return (
            (Decimal(price) - self.average_entry_price)
            / self.average_entry_price
            * Decimal("100")
        )

    @property
    def nxt_return_pct(self) -> Decimal:
        return self.return_pct(self.nxt_price)

    @property
    def krx_expected_return_pct(self) -> Decimal | None:
        if self.krx_expected_open_price is None:
            return None
        return self.return_pct(self.krx_expected_open_price)


@dataclass(frozen=True, slots=True)
class PremarketDecision:
    symbol: str
    generation: int
    action: PremarketAction
    urgency: ExitUrgency
    quantity: int
    policy_version: str
    reason_codes: tuple[str, ...]
    decided_at: datetime

    def to_exit_decision(self) -> ExitDecision:
        action = (
            ExitAction.SELL_MARKET
            if self.action is PremarketAction.PLAN_FULL_EXIT
            else ExitAction.HOLD
        )
        return ExitDecision(
            symbol=self.symbol,
            generation=self.generation,
            action=action,
            urgency=self.urgency,
            quantity=self.quantity if action is ExitAction.SELL_MARKET else 0,
            policy_version=self.policy_version,
            reason_codes=self.reason_codes,
        )


def evaluate_premarket(
    snapshot: PremarketSnapshot,
    *,
    policy: PremarketPolicy,
    now: datetime,
) -> PremarketDecision:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    def decision(
        action: PremarketAction,
        urgency: ExitUrgency,
        quantity: int,
        reason_code: str,
    ) -> PremarketDecision:
        return PremarketDecision(
            symbol=snapshot.symbol,
            generation=snapshot.generation,
            action=action,
            urgency=urgency,
            quantity=quantity,
            policy_version=policy.version,
            reason_codes=(reason_code,),
            decided_at=now,
        )

    if snapshot.sellable_quantity <= 0:
        return decision(
            PremarketAction.HOLD,
            ExitUrgency.NONE,
            0,
            "NO_SELLABLE_QUANTITY",
        )
    age_seconds = (now - snapshot.observed_at).total_seconds()
    if (
        not snapshot.data_fresh
        or age_seconds < 0
        or age_seconds > policy.maximum_snapshot_age_seconds
        or snapshot.nxt_trade_samples < policy.minimum_nxt_trade_samples
    ):
        return decision(
            PremarketAction.HOLD,
            ExitUrgency.NONE,
            0,
            "PREMARKET_DATA_INSUFFICIENT",
        )
    if not policy.approved:
        return decision(
            PremarketAction.HOLD,
            ExitUrgency.NONE,
            0,
            "PREMARKET_POLICY_NOT_APPROVED",
        )

    krx_return = snapshot.krx_expected_return_pct
    executable_returns = [snapshot.nxt_return_pct]
    if krx_return is not None:
        executable_returns.append(krx_return)
    if min(executable_returns) <= Decimal("-7"):
        return decision(
            PremarketAction.PLAN_FULL_EXIT,
            ExitUrgency.HARD_STOP,
            snapshot.sellable_quantity,
            "PREMARKET_HARD_STOP_MINUS_7",
        )

    # NXT alone is never enough for an early protective exit. A KRX expected
    # opening loss is required, then one independent pressure signal confirms it.
    if krx_return is None:
        return decision(
            PremarketAction.HOLD,
            ExitUrgency.NONE,
            0,
            "KRX_EXPECTED_OPEN_REQUIRED",
        )
    pressure_confirmed = (
        snapshot.nxt_sell_pressure >= policy.sell_pressure_threshold
        or snapshot.market_stress_score >= policy.market_stress_threshold
        or snapshot.market_risk is MarketRisk.RISK_OFF
    )
    nxt_confirms_loss = snapshot.nxt_return_pct <= policy.early_loss_pct
    if (
        krx_return <= policy.strong_loss_pct
        and pressure_confirmed
        and nxt_confirms_loss
    ):
        return decision(
            PremarketAction.PLAN_FULL_EXIT,
            ExitUrgency.PROTECTIVE,
            snapshot.sellable_quantity,
            "PREMARKET_STRONG_DEFENSE",
        )
    if (
        krx_return <= policy.early_loss_pct
        and pressure_confirmed
        and nxt_confirms_loss
    ):
        return decision(
            PremarketAction.PLAN_FULL_EXIT,
            ExitUrgency.PROTECTIVE,
            snapshot.sellable_quantity,
            "PREMARKET_EARLY_DEFENSE",
        )
    return decision(
        PremarketAction.HOLD,
        ExitUrgency.NONE,
        0,
        "PREMARKET_HOLD",
    )
