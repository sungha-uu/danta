from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from danta.db.base import Base
from danta.services.runtime_repository import SqlRuntimeRepository
from danta.services.trading_runtime import ManagedPosition


@pytest.mark.asyncio
async def test_closed_generation_is_not_reopened_and_next_generation_is_known() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = SqlRuntimeRepository(factory)
    opened = ManagedPosition(
        symbol="005930",
        generation=0,
        quantity=1,
        sellable_quantity=1,
        average_entry_price=Decimal("100000"),
        opened_at=datetime.now(UTC),
    )
    await repository.save_position(opened)
    await repository.close_position(symbol="005930", generation=0)

    assert await repository.position_average_entry_price(
        symbol="005930",
        generation=0,
    ) == Decimal("100000")
    assert await repository.closed_symbols_since(
        ["005930", "000660"],
        opened_since=opened.opened_at,
    ) == {"005930"}
    assert await repository.latest_generations(["005930", "000660"]) == {"005930": 0}
    await repository.audit(
        "ENTRY_FILL_EMAIL_SENT",
        correlation_id="entry-test",
        payload={"intent_key": "entry-test:005930:BUY:A0"},
    )
    assert await repository.sent_trade_notification_intent_keys() == {"entry-test:005930:BUY:A0"}
    with pytest.raises(RuntimeError, match="cannot be reopened"):
        await repository.save_position(opened)
    await engine.dispose()
