from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from danta.db.models import AuditLogModel, PositionModel
from danta.domain.risk import hard_stop_price
from danta.services.trading_runtime import ManagedPosition


@dataclass(frozen=True, slots=True)
class StoredPosition:
    row_id: str
    symbol: str
    generation: int
    quantity: int
    average_entry_price: Decimal
    opened_at: datetime


class SqlRuntimeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_open_positions(self) -> list[StoredPosition]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(PositionModel).where(PositionModel.status == "OPEN")
                )
            ).all()
            return [
                StoredPosition(
                    row_id=row.id,
                    symbol=row.symbol,
                    generation=row.generation,
                    quantity=row.quantity,
                    average_entry_price=Decimal(row.average_entry_price),
                    opened_at=_aware(row.opened_at),
                )
                for row in rows
            ]

    async def save_position(self, position: ManagedPosition) -> None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(PositionModel).where(
                    PositionModel.symbol == position.symbol,
                    PositionModel.generation == position.generation,
                )
            )
            if row is None:
                row = PositionModel(
                    id=str(uuid4()),
                    symbol=position.symbol,
                    generation=position.generation,
                    quantity=position.quantity,
                    average_entry_price=int(position.average_entry_price),
                    hard_stop_price=hard_stop_price(position.average_entry_price),
                    status="OPEN" if position.quantity > 0 else "CLOSED",
                    opened_at=position.opened_at,
                    closed_at=None,
                )
                session.add(row)
            else:
                row.quantity = position.quantity
                row.average_entry_price = int(position.average_entry_price)
                row.hard_stop_price = hard_stop_price(position.average_entry_price)
                row.status = "OPEN" if position.quantity > 0 else "CLOSED"
                row.closed_at = None if position.quantity > 0 else datetime.now(UTC)
            await session.commit()

    async def close_position(self, *, symbol: str, generation: int) -> None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(PositionModel).where(
                    PositionModel.symbol == symbol,
                    PositionModel.generation == generation,
                )
            )
            if row is None:
                raise RuntimeError("position to close was not found")
            row.quantity = 0
            row.status = "CLOSED"
            row.closed_at = datetime.now(UTC)
            await session.commit()

    async def audit(
        self,
        event_type: str,
        *,
        correlation_id: str | None,
        payload: dict[str, object],
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                AuditLogModel(
                    id=str(uuid4()),
                    event_type=event_type,
                    correlation_id=correlation_id,
                    payload=payload,
                    created_at=datetime.now(UTC),
                )
            )
            await session.commit()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
