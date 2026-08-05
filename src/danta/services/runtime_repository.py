from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select
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
    peak_return_pct: Decimal
    opened_at: datetime


class SqlRuntimeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_open_positions(self) -> list[StoredPosition]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(select(PositionModel).where(PositionModel.status == "OPEN"))
            ).all()
            return [
                StoredPosition(
                    row_id=row.id,
                    symbol=row.symbol,
                    generation=row.generation,
                    quantity=row.quantity,
                    average_entry_price=Decimal(row.average_entry_price),
                    peak_return_pct=Decimal(row.peak_return_pct),
                    opened_at=_aware(row.opened_at),
                )
                for row in rows
            ]

    async def latest_generations(self, symbols: list[str]) -> dict[str, int]:
        if not symbols:
            return {}
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        PositionModel.symbol,
                        func.max(PositionModel.generation),
                    )
                    .where(PositionModel.symbol.in_(symbols))
                    .group_by(PositionModel.symbol)
                )
            ).all()
            return {str(symbol): int(generation) for symbol, generation in rows}

    async def closed_symbols_since(
        self,
        symbols: list[str],
        *,
        opened_since: datetime,
    ) -> set[str]:
        """Return mandate symbols whose lifecycle opened and closed after acceptance."""
        if not symbols:
            return set()
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(PositionModel.symbol).where(
                        PositionModel.symbol.in_(symbols),
                        PositionModel.status == "CLOSED",
                        PositionModel.opened_at >= opened_since,
                    )
                )
            ).all()
            return {str(symbol) for symbol in rows}

    async def position_average_entry_price(
        self,
        *,
        symbol: str,
        generation: int,
    ) -> Decimal | None:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(PositionModel.average_entry_price).where(
                    PositionModel.symbol == symbol,
                    PositionModel.generation == generation,
                )
            )
            return None if value is None else Decimal(value)

    async def sent_trade_notification_intent_keys(self) -> set[str]:
        async with self._session_factory() as session:
            payloads = (
                await session.scalars(
                    select(AuditLogModel.payload).where(
                        AuditLogModel.event_type.in_(
                            ("ENTRY_FILL_EMAIL_SENT", "EXIT_FILL_EMAIL_SENT")
                        )
                    )
                )
            ).all()
        return {
            str(payload["intent_key"])
            for payload in payloads
            if isinstance(payload, dict) and payload.get("intent_key")
        }

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
                    peak_return_pct=position.peak_return_pct,
                    status="OPEN" if position.quantity > 0 else "CLOSED",
                    opened_at=position.opened_at,
                    closed_at=None,
                )
                session.add(row)
            else:
                if row.status == "CLOSED" and position.quantity > 0:
                    raise RuntimeError("closed position generation cannot be reopened")
                row.quantity = position.quantity
                row.average_entry_price = int(position.average_entry_price)
                row.hard_stop_price = hard_stop_price(position.average_entry_price)
                row.peak_return_pct = position.peak_return_pct
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
