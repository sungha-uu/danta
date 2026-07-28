from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol, cast

import httpx

from danta.domain.trading_session import IntentSide, OrderIntent


@dataclass(frozen=True, slots=True)
class BrokerReceipt:
    broker_order_no: str
    order_time: str


class CashOrderBroker(Protocol):
    async def submit_cash_order(
        self,
        *,
        side: str,
        symbol: str,
        quantity: int,
        order_type: str,
        limit_price: int | None = None,
    ) -> BrokerReceipt: ...


class CancelOrderBroker(Protocol):
    async def cancel_cash_order(
        self,
        *,
        broker_order_no: str,
        branch_no: str,
        quantity: int,
    ) -> BrokerReceipt: ...


@dataclass(frozen=True, slots=True)
class OrderExecution:
    idempotency_key: str
    broker_order_no: str
    status: str


@dataclass(frozen=True, slots=True)
class CancellationExecution:
    original_broker_order_no: str
    cancellation_order_no: str
    status: str


class OrderJournal(Protocol):
    async def find(self, idempotency_key: str) -> OrderExecution | None: ...

    async def mark_submitting(self, intent: OrderIntent) -> bool: ...

    async def mark_submitted(
        self, intent: OrderIntent, *, broker_order_no: str
    ) -> OrderExecution: ...

    async def mark_failed(self, intent: OrderIntent, *, reason: str) -> None: ...

    async def mark_unknown(self, intent: OrderIntent, *, reason: str) -> None: ...


class UnknownOrderOutcome(RuntimeError):
    pass


class InMemoryOrderJournal:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._submitting: set[str] = set()
        self._executions: dict[str, OrderExecution] = {}
        self.failures: dict[str, str] = {}

    async def find(self, idempotency_key: str) -> OrderExecution | None:
        async with self._lock:
            return self._executions.get(idempotency_key)

    async def mark_submitting(self, intent: OrderIntent) -> bool:
        async with self._lock:
            key = intent.idempotency_key
            if key in self._submitting or key in self._executions:
                return False
            self._submitting.add(key)
            return True

    async def mark_submitted(
        self, intent: OrderIntent, *, broker_order_no: str
    ) -> OrderExecution:
        async with self._lock:
            execution = OrderExecution(
                idempotency_key=intent.idempotency_key,
                broker_order_no=broker_order_no,
                status="SUBMITTED",
            )
            self._executions[intent.idempotency_key] = execution
            self._submitting.discard(intent.idempotency_key)
            return execution

    async def mark_failed(self, intent: OrderIntent, *, reason: str) -> None:
        async with self._lock:
            self._submitting.discard(intent.idempotency_key)
            self.failures[intent.idempotency_key] = reason

    async def mark_unknown(self, intent: OrderIntent, *, reason: str) -> None:
        async with self._lock:
            self._submitting.add(intent.idempotency_key)
            self.failures[intent.idempotency_key] = f"UNKNOWN:{reason}"


class OrderManager:
    def __init__(self, broker: CashOrderBroker, journal: OrderJournal) -> None:
        self._broker = broker
        self._journal = journal
        self._cancelled_order_numbers: dict[str, CancellationExecution] = {}
        self._cancel_lock = asyncio.Lock()

    async def execute(self, intent: OrderIntent) -> OrderExecution:
        existing = await self._journal.find(intent.idempotency_key)
        if existing is not None:
            return existing
        if intent.side is IntentSide.CANCEL:
            raise NotImplementedError("cancel execution requires a broker order number")
        claimed = await self._journal.mark_submitting(intent)
        if not claimed:
            existing = await self._journal.find(intent.idempotency_key)
            if existing is not None:
                return existing
            raise RuntimeError("order intent is already being submitted")
        try:
            receipt = await self._broker.submit_cash_order(
                side=intent.side.value,
                symbol=intent.symbol,
                quantity=intent.quantity,
                order_type=intent.order_type,
                limit_price=intent.limit_price,
            )
            broker_order_no = str(receipt.broker_order_no)
            if not broker_order_no:
                raise RuntimeError("broker receipt did not include an order number")
            return await self._journal.mark_submitted(
                intent, broker_order_no=broker_order_no
            )
        except (httpx.RequestError, TimeoutError, OSError) as exc:
            await self._journal.mark_unknown(intent, reason=type(exc).__name__)
            raise UnknownOrderOutcome(
                "order outcome is unknown; reconcile before any retry"
            ) from exc
        except BaseException as exc:
            await self._journal.mark_failed(intent, reason=type(exc).__name__)
            raise

    async def cancel(
        self,
        *,
        broker_order_no: str,
        branch_no: str,
        remaining_quantity: int,
    ) -> CancellationExecution:
        if not broker_order_no or not branch_no:
            raise ValueError("broker order number and branch number are required")
        if remaining_quantity <= 0:
            raise ValueError("remaining_quantity must be positive")
        async with self._cancel_lock:
            existing = self._cancelled_order_numbers.get(broker_order_no)
            if existing is not None:
                return existing
            broker = cast(CancelOrderBroker, self._broker)
            receipt = await broker.cancel_cash_order(
                broker_order_no=broker_order_no,
                branch_no=branch_no,
                quantity=remaining_quantity,
            )
            execution = CancellationExecution(
                original_broker_order_no=broker_order_no,
                cancellation_order_no=receipt.broker_order_no,
                status="CANCEL_SUBMITTED",
            )
            self._cancelled_order_numbers[broker_order_no] = execution
            return execution
