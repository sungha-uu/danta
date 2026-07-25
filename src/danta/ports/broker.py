from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: int
    change_rate: Decimal | None
    raw_timestamp: str | None


@dataclass(frozen=True, slots=True)
class AccountPosition:
    symbol: str
    quantity: int
    sellable_quantity: int
    average_price: Decimal


class BrokerMarketData(Protocol):
    async def current_price(self, symbol: str) -> Quote: ...


class BrokerAccount(Protocol):
    async def positions(self) -> list[AccountPosition]: ...


class BrokerOrderExecutor(Protocol):
    async def submit_approved_buy(self, approval_id: str) -> str: ...

    async def submit_protective_market_sell(
        self, *, symbol: str, quantity: int, idempotency_key: str
    ) -> str: ...

