from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class CapitalReservation:
    reservation_id: str
    mandate_id: str
    symbol: str
    amount: int


@dataclass(slots=True)
class _MandateBudget:
    account_cash_snapshot: int
    symbol_caps: dict[str, int]
    reservations: dict[str, CapitalReservation]
    consumed_by_symbol: dict[str, int]


class CapitalAllocator:
    """Atomically protects one account cash snapshot from concurrent entry decisions."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._budgets: dict[str, _MandateBudget] = {}

    async def register_mandate(
        self,
        *,
        mandate_id: str,
        orderable_cash: int,
        allocations: dict[str, Decimal],
    ) -> None:
        if not mandate_id:
            raise ValueError("mandate_id is required")
        if orderable_cash <= 0:
            raise ValueError("orderable_cash must be positive")
        if not allocations:
            raise ValueError("allocations must not be empty")
        total = sum(allocations.values(), Decimal("0"))
        if total <= 0 or total > Decimal("100"):
            raise ValueError("allocation total must be between 0 and 100")
        caps = {
            symbol: int(Decimal(orderable_cash) * allocation / Decimal("100"))
            for symbol, allocation in allocations.items()
        }
        if any(cap <= 0 for cap in caps.values()):
            raise ValueError("every allocation must reserve positive cash")
        async with self._lock:
            existing = self._budgets.get(mandate_id)
            if existing is not None:
                if (
                    existing.account_cash_snapshot != orderable_cash
                    or existing.symbol_caps != caps
                ):
                    raise ValueError("mandate budget is already registered with different values")
                return
            self._budgets[mandate_id] = _MandateBudget(
                account_cash_snapshot=orderable_cash,
                symbol_caps=caps,
                reservations={},
                consumed_by_symbol={symbol: 0 for symbol in caps},
            )

    async def reserve(
        self,
        *,
        mandate_id: str,
        symbol: str,
        amount: int,
        reservation_id: str,
    ) -> CapitalReservation:
        if amount <= 0:
            raise ValueError("reservation amount must be positive")
        async with self._lock:
            budget = self._required_budget(mandate_id)
            existing = budget.reservations.get(reservation_id)
            if existing is not None:
                if existing.symbol != symbol or existing.amount != amount:
                    raise ValueError("reservation id is already used with different values")
                return existing
            if symbol not in budget.symbol_caps:
                raise ValueError("symbol is outside mandate allocation")
            reserved_for_symbol = sum(
                item.amount for item in budget.reservations.values() if item.symbol == symbol
            )
            used = budget.consumed_by_symbol[symbol] + reserved_for_symbol
            if used + amount > budget.symbol_caps[symbol]:
                raise ValueError("reservation exceeds symbol allocation cap")
            all_reserved = sum(item.amount for item in budget.reservations.values())
            all_consumed = sum(budget.consumed_by_symbol.values())
            if all_reserved + all_consumed + amount > budget.account_cash_snapshot:
                raise ValueError("reservation exceeds account cash snapshot")
            reservation = CapitalReservation(
                reservation_id=reservation_id,
                mandate_id=mandate_id,
                symbol=symbol,
                amount=amount,
            )
            budget.reservations[reservation_id] = reservation
            return reservation

    async def consume(self, reservation_id: str, *, amount: int) -> None:
        if amount < 0:
            raise ValueError("consumed amount must not be negative")
        async with self._lock:
            budget, reservation = self._find_reservation(reservation_id)
            if amount > reservation.amount:
                raise ValueError("consumed amount exceeds reservation")
            budget.consumed_by_symbol[reservation.symbol] += amount
            del budget.reservations[reservation_id]

    async def consume_partial(self, reservation_id: str, *, amount: int) -> None:
        if amount <= 0:
            raise ValueError("consumed amount must be positive")
        async with self._lock:
            budget, reservation = self._find_reservation(reservation_id)
            if amount > reservation.amount:
                raise ValueError("consumed amount exceeds reservation")
            budget.consumed_by_symbol[reservation.symbol] += amount
            remaining = reservation.amount - amount
            if remaining == 0:
                del budget.reservations[reservation_id]
            else:
                budget.reservations[reservation_id] = CapitalReservation(
                    reservation_id=reservation.reservation_id,
                    mandate_id=reservation.mandate_id,
                    symbol=reservation.symbol,
                    amount=remaining,
                )

    async def release(self, reservation_id: str) -> bool:
        async with self._lock:
            for budget in self._budgets.values():
                if reservation_id in budget.reservations:
                    del budget.reservations[reservation_id]
                    return True
        return False

    async def available_for_symbol(self, mandate_id: str, symbol: str) -> int:
        async with self._lock:
            budget = self._required_budget(mandate_id)
            if symbol not in budget.symbol_caps:
                raise ValueError("symbol is outside mandate allocation")
            reserved = sum(
                item.amount for item in budget.reservations.values() if item.symbol == symbol
            )
            return (
                budget.symbol_caps[symbol]
                - budget.consumed_by_symbol[symbol]
                - reserved
            )

    def _required_budget(self, mandate_id: str) -> _MandateBudget:
        try:
            return self._budgets[mandate_id]
        except KeyError as exc:
            raise ValueError("mandate budget is not registered") from exc

    def _find_reservation(
        self, reservation_id: str
    ) -> tuple[_MandateBudget, CapitalReservation]:
        for budget in self._budgets.values():
            reservation = budget.reservations.get(reservation_id)
            if reservation is not None:
                return budget, reservation
        raise ValueError("capital reservation was not found")
