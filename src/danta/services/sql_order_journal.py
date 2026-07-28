from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from danta.db.models import BrokerOrderModel, OrderIntentModel
from danta.domain.trading_session import OrderIntent
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
