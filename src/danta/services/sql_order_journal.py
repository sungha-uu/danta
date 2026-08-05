from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from danta.db.models import BrokerOrderModel, FillModel, OrderIntentModel
from danta.domain.trading_session import (
    IntentPriority,
    IntentSide,
    OrderIntent,
)
from danta.services.order_manager import OrderExecution


class SqlOrderJournal:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find(self, idempotency_key: str) -> OrderExecution | None:
        async with self._session_factory() as session:
            intent = await session.scalar(
                select(OrderIntentModel).where(
                    OrderIntentModel.idempotency_key == idempotency_key
                )
            )
            if intent is None or intent.status != "SUBMITTED":
                return None
            order = await session.scalar(
                select(BrokerOrderModel)
                .where(BrokerOrderModel.order_intent_id == intent.id)
                .order_by(BrokerOrderModel.created_at.desc())
            )
            if order is None or order.broker_order_no is None:
                return None
            return OrderExecution(
                idempotency_key=idempotency_key,
                broker_order_no=order.broker_order_no,
                status=order.status,
            )

    async def load_recoverable(self) -> list[RecoveredOrder]:
        """Load orders whose terminal broker outcome still matters."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(OrderIntentModel, BrokerOrderModel)
                    .outerjoin(
                        BrokerOrderModel,
                        BrokerOrderModel.order_intent_id == OrderIntentModel.id,
                    )
                    .where(
                        (
                            OrderIntentModel.status.in_(
                                ("SUBMITTING", "SUBMITTED", "PARTIALLY_FILLED")
                            )
                        )
                        | OrderIntentModel.status.like("UNKNOWN:%")
                    )
                    .order_by(
                        OrderIntentModel.created_at,
                        BrokerOrderModel.created_at.desc(),
                    )
                )
            ).all()
        seen: set[str] = set()
        recovered: list[RecoveredOrder] = []
        for intent_row, broker_row in rows:
            if intent_row.idempotency_key in seen:
                continue
            seen.add(intent_row.idempotency_key)
            recovered.append(
                RecoveredOrder(
                    intent=OrderIntent(
                        idempotency_key=intent_row.idempotency_key,
                        symbol=intent_row.symbol,
                        generation=intent_row.generation,
                        side=IntentSide(intent_row.side),
                        priority=IntentPriority(intent_row.priority),
                        quantity=intent_row.quantity,
                        order_type=intent_row.order_type,
                        limit_price=intent_row.limit_price,
                        cause=intent_row.cause,
                        policy_version=intent_row.policy_version,
                        created_at=_aware(intent_row.created_at),
                        approval_id=intent_row.approval_id,
                    ),
                    intent_status=intent_row.status,
                    broker_order_no=(
                        None if broker_row is None else broker_row.broker_order_no
                    ),
                    broker_status=(
                        None if broker_row is None else broker_row.status
                    ),
                )
            )
        return recovered

    async def apply_broker_status(
        self,
        *,
        idempotency_key: str,
        broker_order_no: str,
        symbol: str,
        ordered_quantity: int,
        cumulative_filled_quantity: int,
        average_fill_price: Decimal,
        remaining_quantity: int,
        filled_at: datetime,
    ) -> int:
        """Persist the incremental fill represented by a cumulative KIS status.

        KIS order inquiry returns cumulative quantity and average price.  The local
        ledger stores only the positive delta and uses a unique cumulative watermark
        so restart reconciliation cannot duplicate an already recorded fill.
        """
        if ordered_quantity <= 0:
            raise ValueError("ordered quantity must be positive")
        if not 0 <= cumulative_filled_quantity <= ordered_quantity:
            raise ValueError("cumulative fill quantity is invalid")
        if remaining_quantity < 0:
            raise ValueError("remaining quantity cannot be negative")
        if cumulative_filled_quantity > 0 and average_fill_price <= 0:
            raise ValueError("filled order must include a positive average price")

        async with self._session_factory() as session:
            row = await session.scalar(
                select(OrderIntentModel).where(
                    OrderIntentModel.idempotency_key == idempotency_key
                )
            )
            if row is None:
                raise RuntimeError("order intent was not found for fill persistence")
            broker_order = await session.scalar(
                select(BrokerOrderModel)
                .where(
                    BrokerOrderModel.order_intent_id == row.id,
                    BrokerOrderModel.broker_order_no == broker_order_no,
                )
                .order_by(BrokerOrderModel.created_at.desc())
            )
            if broker_order is None:
                raise RuntimeError("broker order was not found for fill persistence")

            recorded_quantity = int(
                await session.scalar(
                    select(func.coalesce(func.sum(FillModel.quantity), 0)).where(
                        FillModel.broker_order_id == broker_order.id
                    )
                )
                or 0
            )
            delta = cumulative_filled_quantity - recorded_quantity
            if delta < 0:
                raise RuntimeError("broker cumulative fill moved behind local ledger")
            if delta > 0:
                recorded_value = await session.scalar(
                    select(
                        func.coalesce(func.sum(FillModel.price * FillModel.quantity), 0)
                    ).where(FillModel.broker_order_id == broker_order.id)
                )
                cumulative_value = average_fill_price * Decimal(
                    cumulative_filled_quantity
                )
                incremental_value = cumulative_value - Decimal(recorded_value or 0)
                if incremental_value <= 0:
                    raise RuntimeError("incremental fill value must be positive")
                incremental_price = int(
                    (incremental_value / Decimal(delta)).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                session.add(
                    FillModel(
                        id=str(uuid4()),
                        broker_order_id=broker_order.id,
                        symbol=symbol,
                        price=incremental_price,
                        quantity=delta,
                        cumulative_quantity=cumulative_filled_quantity,
                        filled_at=filled_at,
                    )
                )

            if remaining_quantity > 0:
                status = (
                    "PARTIALLY_FILLED"
                    if cumulative_filled_quantity > 0
                    else "SUBMITTED"
                )
            elif cumulative_filled_quantity > 0:
                status = "FILLED"
            else:
                status = "CLOSED"
            row.status = status
            broker_order.status = status
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                # A concurrent/replayed cumulative watermark is already durable.
                return 0
            return delta

    async def mark_submitting(self, intent: OrderIntent) -> bool:
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(OrderIntentModel).where(
                    OrderIntentModel.idempotency_key == intent.idempotency_key
                )
            )
            if existing is not None:
                if existing.status.startswith("FAILED:"):
                    existing.status = "SUBMITTING"
                    existing.created_at = intent.created_at
                    await session.commit()
                    return True
                return False
            row = OrderIntentModel(
                id=str(uuid4()),
                idempotency_key=intent.idempotency_key,
                approval_id=intent.approval_id,
                symbol=intent.symbol,
                side=intent.side.value,
                cause=intent.cause,
                quantity=intent.quantity,
                generation=intent.generation,
                priority=int(intent.priority),
                order_type=intent.order_type,
                limit_price=intent.limit_price,
                policy_version=intent.policy_version,
                status="SUBMITTING",
                created_at=intent.created_at,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            return True

    async def mark_submitted(
        self, intent: OrderIntent, *, broker_order_no: str
    ) -> OrderExecution:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(OrderIntentModel).where(
                    OrderIntentModel.idempotency_key == intent.idempotency_key
                )
            )
            if row is None:
                raise RuntimeError("submitting order intent was not found")
            row.status = "SUBMITTED"
            broker_order = BrokerOrderModel(
                id=str(uuid4()),
                order_intent_id=row.id,
                broker_order_no=broker_order_no,
                status="SUBMITTED",
                raw_response_hash=None,
                created_at=datetime.now(UTC),
            )
            session.add(broker_order)
            await session.commit()
            return OrderExecution(
                idempotency_key=intent.idempotency_key,
                broker_order_no=broker_order_no,
                status="SUBMITTED",
            )

    async def mark_failed(self, intent: OrderIntent, *, reason: str) -> None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(OrderIntentModel).where(
                    OrderIntentModel.idempotency_key == intent.idempotency_key
                )
            )
            if row is not None:
                row.status = f"FAILED:{reason}"[:24]
                await session.commit()

    async def mark_unknown(self, intent: OrderIntent, *, reason: str) -> None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(OrderIntentModel).where(
                    OrderIntentModel.idempotency_key == intent.idempotency_key
                )
            )
            if row is not None:
                row.status = f"UNKNOWN:{reason}"[:24]
                await session.commit()


@dataclass(frozen=True, slots=True)
class RecoveredOrder:
    intent: OrderIntent
    intent_status: str
    broker_order_no: str | None
    broker_status: str | None


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
