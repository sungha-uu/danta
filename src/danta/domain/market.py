from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum


class MarketRisk(StrEnum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    RISK_OFF = "RISK_OFF"


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    symbol: str
    observed_at: datetime
    last_price: int
    best_bid: int | None
    best_ask: int | None
    sell_pressure_score: Decimal
    stabilization_score: Decimal
    buy_recovery_score: Decimal
    weakness_score: Decimal
    market_stress_score: Decimal
    market_risk: MarketRisk = MarketRisk.NORMAL
    box_valid: bool = True
    data_fresh: bool = True
    vwap: Decimal | None = None

    def __post_init__(self) -> None:
        if len(self.symbol) != 6 or not self.symbol.isascii() or not self.symbol.isalnum():
            raise ValueError("symbol must be six ASCII alphanumeric characters")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.last_price <= 0:
            raise ValueError("last_price must be positive")
        if self.best_bid is not None and self.best_bid <= 0:
            raise ValueError("best_bid must be positive")
        if self.best_ask is not None and self.best_ask <= 0:
            raise ValueError("best_ask must be positive")
        for name in (
            "sell_pressure_score",
            "stabilization_score",
            "buy_recovery_score",
            "weakness_score",
            "market_stress_score",
        ):
            value = getattr(self, name)
            if value < 0 or value > 1:
                raise ValueError(f"{name} must be between 0 and 1")

    @property
    def spread_bps(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        midpoint = (Decimal(self.best_bid) + Decimal(self.best_ask)) / Decimal("2")
        return (Decimal(self.best_ask - self.best_bid) / midpoint) * Decimal("10000")

    def is_fresh(self, *, now: datetime, max_age_seconds: int) -> bool:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        age = now.astimezone(UTC) - self.observed_at.astimezone(UTC)
        return self.data_fresh and age.total_seconds() <= max_age_seconds
