from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from danta.domain.trading_session import SymbolSession, SymbolState
from danta.ports.broker import AccountPosition


@dataclass(frozen=True, slots=True)
class ReconciliationIssue:
    symbol: str
    code: str
    broker_quantity: int
    internal_quantity: int


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    issues: tuple[ReconciliationIssue, ...]
    discovered_positions: tuple[AccountPosition, ...]

    @property
    def safe_for_new_entries(self) -> bool:
        return not self.issues


def reconcile_positions(
    broker_positions: list[AccountPosition],
    sessions: dict[str, SymbolSession],
) -> ReconciliationResult:
    broker_by_symbol = {position.symbol: position for position in broker_positions}
    issues: list[ReconciliationIssue] = []
    discovered: list[AccountPosition] = []
    for symbol, position in broker_by_symbol.items():
        session = sessions.get(symbol)
        if session is None:
            discovered.append(position)
            issues.append(
                ReconciliationIssue(symbol, "BROKER_ONLY_POSITION", position.quantity, 0)
            )
            continue
        if session.quantity != position.quantity:
            issues.append(
                ReconciliationIssue(
                    symbol,
                    "POSITION_QUANTITY_MISMATCH",
                    position.quantity,
                    session.quantity,
                )
            )
    for symbol, session in sessions.items():
        if (
            session.state
            in {
                SymbolState.POSITION_OPEN,
                SymbolState.PARTIALLY_FILLED,
                SymbolState.SELL_PENDING,
            }
            and symbol not in broker_by_symbol
        ):
            issues.append(
                ReconciliationIssue(symbol, "INTERNAL_ONLY_POSITION", 0, session.quantity)
            )
    return ReconciliationResult(tuple(issues), tuple(discovered))


def recovered_session(position: AccountPosition, *, generation: int) -> SymbolSession:
    if position.average_price <= Decimal("0"):
        raise ValueError("broker position average price must be positive")
    return SymbolSession(
        symbol=position.symbol,
        generation=generation,
        state=SymbolState.POSITION_OPEN,
        quantity=position.quantity,
        sellable_quantity=position.sellable_quantity,
    )
