from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from danta.db.base import Base
from danta.domain.trading_session import (
    IntentPriority,
    IntentSide,
    OrderIntent,
)
from danta.services.sql_order_journal import SqlOrderJournal


def _intent() -> OrderIntent:
    return OrderIntent(
        idempotency_key="m1:005930:BUY",
        symbol="005930",
        generation=0,
        side=IntentSide.BUY,
        priority=IntentPriority.ENTRY,
        quantity=10,
        order_type="LIMIT",
        limit_price=70000,
        cause="ENTRY_CONFIRMED",
        policy_version="entry-v1",
        created_at=datetime.now(UTC),
        approval_id="m1",
    )


async def test_sql_journal_persists_idempotency(tmp_path: object) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    journal = SqlOrderJournal(factory)
    intent = _intent()
    assert await journal.mark_submitting(intent)
    assert not await journal.mark_submitting(intent)
    submitted = await journal.mark_submitted(intent, broker_order_no="12345")
    assert submitted.broker_order_no == "12345"
    assert await journal.find(intent.idempotency_key) == submitted
    await engine.dispose()


async def test_sql_journal_persists_cumulative_fills_once() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    journal = SqlOrderJournal(factory)
    intent = _intent()
    assert await journal.mark_submitting(intent)
    await journal.mark_submitted(intent, broker_order_no="12345")
    observed = datetime.now(UTC)
    assert await journal.apply_broker_status(
        idempotency_key=intent.idempotency_key,
        broker_order_no="12345",
        symbol="005930",
        ordered_quantity=10,
        cumulative_filled_quantity=4,
        average_fill_price=70000,
        remaining_quantity=6,
        filled_at=observed,
    ) == 4
    assert await journal.apply_broker_status(
        idempotency_key=intent.idempotency_key,
        broker_order_no="12345",
        symbol="005930",
        ordered_quantity=10,
        cumulative_filled_quantity=10,
        average_fill_price=70100,
        remaining_quantity=0,
        filled_at=observed,
    ) == 6
    assert await journal.apply_broker_status(
        idempotency_key=intent.idempotency_key,
        broker_order_no="12345",
        symbol="005930",
        ordered_quantity=10,
        cumulative_filled_quantity=10,
        average_fill_price=70100,
        remaining_quantity=0,
        filled_at=observed,
    ) == 0
    await engine.dispose()
