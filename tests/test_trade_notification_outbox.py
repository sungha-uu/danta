from datetime import UTC
from decimal import Decimal

from danta.services.trade_notification_outbox import (
    TradeNotificationKind,
    TradeNotificationOutbox,
)


def test_buy_notification_survives_restart_and_is_idempotent(tmp_path) -> None:
    first = TradeNotificationOutbox(tmp_path / "notifications")
    assert first.enqueue_buy(
        intent_key="entry:005930:BUY:A0",
        correlation_id="entry",
        name="삼성전자",
        price=100_000,
        quantity=10,
    )
    assert not first.enqueue_buy(
        intent_key="entry:005930:BUY:A0",
        correlation_id="entry",
        name="삼성전자",
        price=100_000,
        quantity=10,
    )

    restarted = TradeNotificationOutbox(tmp_path / "notifications")
    pending = restarted.load_pending()
    assert len(pending) == 1
    assert pending[0].created_at.tzinfo is UTC
    assert pending[0].quantity == 10

    restarted.mark_sent(
        kind=TradeNotificationKind.BUY,
        intent_key="entry:005930:BUY:A0",
    )
    assert restarted.load_pending() == []
    assert not restarted.enqueue_buy(
        intent_key="entry:005930:BUY:A0",
        correlation_id="entry",
        name="삼성전자",
        price=100_000,
        quantity=10,
    )


def test_exit_notification_preserves_return_and_cause(tmp_path) -> None:
    outbox = TradeNotificationOutbox(tmp_path / "notifications")
    assert outbox.enqueue_exit(
        intent_key="entry:005930:SELL:1",
        correlation_id="entry",
        name="삼성전자",
        price=95_000,
        return_pct=Decimal("-5.0"),
        cause="HARD_DEFENSE_MINUS_5",
    )

    item = outbox.load_pending()[0]
    assert item.kind is TradeNotificationKind.EXIT
    assert item.return_pct == Decimal("-5.0")
    assert item.cause == "HARD_DEFENSE_MINUS_5"
