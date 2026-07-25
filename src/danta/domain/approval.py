from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass(frozen=True, slots=True)
class BuyApproval:
    approval_id: str
    symbol: str
    max_amount_krw: int
    order_type: OrderType
    expires_at: datetime
    max_holding_days: int
    limit_price: int | None = None
    max_acceptable_price: int | None = None

    def validate_for_order(
        self,
        *,
        now: datetime,
        symbol: str,
        expected_amount_krw: int,
        current_price: int,
        is_watched: bool,
        already_used: bool,
    ) -> None:
        errors: list[str] = []
        if already_used:
            errors.append("approval already used")
        if now >= self.expires_at:
            errors.append("approval expired")
        if symbol != self.symbol:
            errors.append("symbol outside approval")
        if not is_watched:
            errors.append("symbol is not in active watch selection")
        if expected_amount_krw <= 0 or expected_amount_krw > self.max_amount_krw:
            errors.append("amount outside approval")
        if self.max_acceptable_price and current_price > self.max_acceptable_price:
            errors.append("current price exceeds approved maximum")
        if self.order_type is OrderType.LIMIT and not self.limit_price:
            errors.append("limit order requires limit_price")
        if self.max_holding_days < 1:
            errors.append("max_holding_days must be positive")
        if errors:
            raise ValueError("; ".join(errors))

