from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

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
