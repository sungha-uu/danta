from datetime import UTC, datetime

from danta.domain.trading_session import (
    IntentPriority,
    IntentSide,
    OrderIntent,
)
from danta.services.order_manager import InMemoryOrderJournal, OrderManager


class _Receipt:
    broker_order_no = "12345"


class FakeBroker:
    def __init__(self) -> None:
        self.calls = 0

    async def submit_cash_order(self, **_: object) -> _Receipt:
        self.calls += 1
        return _Receipt()

    async def cancel_cash_order(self, **_: object) -> _Receipt:
        self.calls += 1
        return _Receipt()


def _intent() -> OrderIntent:
    return OrderIntent(
        idempotency_key="entry:005930",
        symbol="005930",
        generation=1,
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


async def test_order_manager_submits_duplicate_intent_once() -> None:
    broker = FakeBroker()
    manager = OrderManager(broker, InMemoryOrderJournal())
    first = await manager.execute(_intent())
    second = await manager.execute(_intent())
    assert first == second
    assert broker.calls == 1


async def test_order_manager_submits_duplicate_cancel_once() -> None:
    broker = FakeBroker()
    manager = OrderManager(broker, InMemoryOrderJournal())
    first = await manager.cancel(
        broker_order_no="10", branch_no="1", remaining_quantity=3
    )
    second = await manager.cancel(
        broker_order_no="10", branch_no="1", remaining_quantity=3
    )
    assert first == second
    assert broker.calls == 1
