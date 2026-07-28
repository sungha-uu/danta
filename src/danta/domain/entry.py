from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from danta.domain.market import MarketRisk, MarketSnapshot


class EntryAction(StrEnum):
    WAIT_PRICE = "WAIT_PRICE"
    WAIT_SELL_PRESSURE = "WAIT_SELL_PRESSURE"
    WAIT_STABILIZATION = "WAIT_STABILIZATION"
    SUBMIT_LIMIT_BUY = "SUBMIT_LIMIT_BUY"
    INVALIDATE_MANDATE = "INVALIDATE_MANDATE"
    BLOCK = "BLOCK"


@dataclass(frozen=True, slots=True)
class EntryPolicy:
    version: str
    approved: bool
    max_snapshot_age_seconds: int
    sell_pressure_block: Decimal
    stabilization_required: Decimal
    buy_recovery_required: Decimal
    max_spread_bps: Decimal

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("entry policy version is required")
        if self.max_snapshot_age_seconds <= 0:
            raise ValueError("max_snapshot_age_seconds must be positive")
        for name in (
            "sell_pressure_block",
            "stabilization_required",
            "buy_recovery_required",
        ):
            value = getattr(self, name)
            if value < 0 or value > 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.max_spread_bps <= 0:
            raise ValueError("max_spread_bps must be positive")


@dataclass(frozen=True, slots=True)
class EntryDecision:
    symbol: str
    action: EntryAction
    policy_version: str
    limit_price: int | None
    reason_codes: tuple[str, ...]


def evaluate_entry(
    snapshot: MarketSnapshot,
    *,
    maximum_price: int,
    policy: EntryPolicy,
    snapshot_is_fresh: bool,
) -> EntryDecision:
    if maximum_price <= 0:
        raise ValueError("maximum_price must be positive")
    if not policy.approved:
        return EntryDecision(
            snapshot.symbol,
            EntryAction.BLOCK,
            policy.version,
            None,
            ("ENTRY_POLICY_NOT_APPROVED",),
        )
    if not snapshot_is_fresh:
        return EntryDecision(
            snapshot.symbol,
            EntryAction.BLOCK,
            policy.version,
            None,
            ("MARKET_DATA_STALE",),
        )
    if snapshot.market_risk is MarketRisk.RISK_OFF:
        return EntryDecision(
            snapshot.symbol,
            EntryAction.BLOCK,
            policy.version,
            None,
            ("MARKET_RISK_OFF",),
        )
    if snapshot.best_ask is None:
        return EntryDecision(
            snapshot.symbol,
            EntryAction.BLOCK,
            policy.version,
            None,
            ("BEST_ASK_UNAVAILABLE",),
        )
    if snapshot.best_ask > maximum_price:
        return EntryDecision(
            snapshot.symbol,
            EntryAction.WAIT_PRICE,
            policy.version,
            None,
            ("BEST_ASK_ABOVE_MAXIMUM_PRICE",),
        )
    spread_bps = snapshot.spread_bps
    if spread_bps is None or spread_bps > policy.max_spread_bps:
        return EntryDecision(
            snapshot.symbol,
            EntryAction.WAIT_STABILIZATION,
            policy.version,
            None,
            ("SPREAD_TOO_WIDE",),
        )
    if snapshot.sell_pressure_score >= policy.sell_pressure_block:
        return EntryDecision(
            snapshot.symbol,
            EntryAction.WAIT_SELL_PRESSURE,
            policy.version,
            None,
            ("SELL_PRESSURE_STRONG",),
        )
    if snapshot.stabilization_score < policy.stabilization_required:
        return EntryDecision(
            snapshot.symbol,
            EntryAction.WAIT_STABILIZATION,
            policy.version,
            None,
            ("PRICE_NOT_STABILIZED",),
        )
    if snapshot.buy_recovery_score < policy.buy_recovery_required:
        return EntryDecision(
            snapshot.symbol,
            EntryAction.WAIT_STABILIZATION,
            policy.version,
            None,
            ("BUY_PRESSURE_NOT_RECOVERED",),
        )
    return EntryDecision(
        snapshot.symbol,
        EntryAction.SUBMIT_LIMIT_BUY,
        policy.version,
        min(snapshot.best_ask, maximum_price),
        ("PRICE_ELIGIBLE", "STABILIZED", "BUY_PRESSURE_RECOVERED"),
    )
