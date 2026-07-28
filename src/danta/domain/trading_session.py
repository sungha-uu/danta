from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum


class OrchestratorState(StrEnum):
    BOOTING = "BOOTING"
    RECONCILING = "RECONCILING"
    RUNNING = "RUNNING"
    ENTRY_BLOCKED = "ENTRY_BLOCKED"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"


class SymbolState(StrEnum):
    IDLE = "IDLE"
    WATCHING_ENTRY = "WATCHING_ENTRY"
    BUY_PENDING = "BUY_PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    POSITION_OPEN = "POSITION_OPEN"
    SELL_PENDING = "SELL_PENDING"
    CLOSED = "CLOSED"
    INVALIDATED = "INVALIDATED"
    QUARANTINED = "QUARANTINED"


class IntentPriority(IntEnum):
    HARD_STOP_EXIT = 0
    PROTECTIVE_EXIT = 10
    PROFIT_OR_TIME_EXIT = 20
    CANCEL = 30
    RECONCILE = 40
    ENTRY = 50
    AUXILIARY = 60


class IntentSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    CANCEL = "CANCEL"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    idempotency_key: str
    symbol: str
    generation: int
    side: IntentSide
    priority: IntentPriority
    quantity: int
    order_type: str
    limit_price: int | None
    cause: str
    policy_version: str
    created_at: datetime
    approval_id: str | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            raise ValueError("idempotency_key is required")
        if self.quantity <= 0 and self.side is not IntentSide.CANCEL:
            raise ValueError("quantity must be positive")
        if self.generation < 0:
            raise ValueError("generation must not be negative")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if self.side is IntentSide.BUY and self.order_type != "LIMIT":
            raise ValueError("buy intents must use LIMIT orders")
        if self.order_type == "LIMIT" and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("limit orders require a positive limit_price")


@dataclass(slots=True)
class SymbolSession:
    symbol: str
    generation: int
    state: SymbolState
    last_sequence: int = 0
    active_order_key: str | None = None
    quantity: int = 0
    sellable_quantity: int = 0

    def apply_sequence(self, sequence: int) -> bool:
        if sequence <= self.last_sequence:
            return False
        self.last_sequence = sequence
        return True
