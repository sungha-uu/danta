import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from danta.adapters.kis.client import KisApiError
from danta.domain.trading_session import IntentSide
from danta.services import paper_trading_application as application_module
from danta.services.paper_trading_application import PaperTradingApplication


class _RateLimitedBroker:
    async def daily_order_statuses(self, **_: Any) -> list[object]:
        raise KisApiError("EGW00201", status_code=429)


class _AuditRepository:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, dict[str, object]]] = []

    async def audit(
        self,
        event_type: str,
        *,
        correlation_id: str | None,
        payload: dict[str, object],
    ) -> None:
        self.events.append((event_type, correlation_id, payload))


async def test_order_status_rate_limit_is_isolated_and_backed_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = object.__new__(PaperTradingApplication)
    application.settings = SimpleNamespace(
        order_poll_interval_seconds=Decimal("2.0")
    )
    application.mandate = SimpleNamespace(command_id="entry-test")
    repository = _AuditRepository()

    async def stop_after_backoff(seconds: float) -> None:
        assert seconds == 2.0
        raise asyncio.CancelledError

    monkeypatch.setattr(application_module.asyncio, "sleep", stop_after_backoff)

    with pytest.raises(asyncio.CancelledError):
        await application._poll_orders(
            None,  # type: ignore[arg-type]
            _RateLimitedBroker(),  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            repository,  # type: ignore[arg-type]
            asyncio.Queue(),
            {},
        )

    assert repository.events == [
        (
            "KIS_ORDER_STATUS_POLL_ERROR",
            "entry-test",
            {
                "error": "KisApiError",
                "status_code": 429,
                "retry_seconds": 2.0,
            },
        )
    ]


async def test_entry_price_notification_is_queued_once_per_intent() -> None:
    application = object.__new__(PaperTradingApplication)
    application.mandate = SimpleNamespace(command_id="entry-test")
    application._notified_price_intents = set()
    repository = _AuditRepository()
    queue: asyncio.Queue[tuple[str, str, int]] = asyncio.Queue()
    intent = SimpleNamespace(
        side=IntentSide.BUY,
        limit_price=1_445_000,
        idempotency_key="entry-test:000660:BUY:A0",
        symbol="000660",
    )

    await application._queue_entry_price_notification(
        intent,  # type: ignore[arg-type]
        queue,
        {"000660": "SK하이닉스"},
        repository,  # type: ignore[arg-type]
    )
    await application._queue_entry_price_notification(
        intent,  # type: ignore[arg-type]
        queue,
        {"000660": "SK하이닉스"},
        repository,  # type: ignore[arg-type]
    )

    assert queue.qsize() == 1
    assert await queue.get() == (
        "entry-test:000660:BUY:A0",
        "SK하이닉스",
        1_445_000,
    )
    assert [event[0] for event in repository.events] == [
        "ENTRY_LIMIT_PRICE_DETERMINED"
    ]


async def test_hard_stop_final_fill_queues_one_notification() -> None:
    application = object.__new__(PaperTradingApplication)
    application.mandate = SimpleNamespace(command_id="entry-test")
    application._notified_stop_intents = set()
    repository = _AuditRepository()
    queue: asyncio.Queue[tuple[str, str, int, Decimal]] = asyncio.Queue()
    intent = SimpleNamespace(
        side=IntentSide.SELL,
        cause="HARD_STOP_MINUS_7",
        idempotency_key="entry-test:000660:SELL:1",
        symbol="000660",
    )
    status = SimpleNamespace(
        remaining_quantity=0,
        filled_quantity=10,
        average_fill_price=Decimal("1479300"),
    )
    position = SimpleNamespace(average_entry_price=Decimal("1590000"))

    await application._queue_stop_loss_notification(
        intent,  # type: ignore[arg-type]
        status,  # type: ignore[arg-type]
        position,  # type: ignore[arg-type]
        queue,
        {"000660": "SK하이닉스"},
        repository,  # type: ignore[arg-type]
    )
    await application._queue_stop_loss_notification(
        intent,  # type: ignore[arg-type]
        status,  # type: ignore[arg-type]
        position,  # type: ignore[arg-type]
        queue,
        {"000660": "SK하이닉스"},
        repository,  # type: ignore[arg-type]
    )

    intent_key, name, price, return_pct = await queue.get()
    assert queue.empty()
    assert intent_key == "entry-test:000660:SELL:1"
    assert name == "SK하이닉스"
    assert price == 1_479_300
    assert return_pct.quantize(Decimal("0.01")) == Decimal("-6.96")
    assert [event[0] for event in repository.events] == [
        "HARD_STOP_EMAIL_QUEUED"
    ]


async def test_minus_five_defense_final_fill_queues_notification() -> None:
    application = object.__new__(PaperTradingApplication)
    application.mandate = SimpleNamespace(command_id="entry-test")
    application._notified_stop_intents = set()
    repository = _AuditRepository()
    queue: asyncio.Queue[tuple[str, str, int, Decimal]] = asyncio.Queue()
    intent = SimpleNamespace(
        side=IntentSide.SELL,
        cause="HARD_DEFENSE_MINUS_5",
        idempotency_key="entry-test:005930:SELL:minus5",
        symbol="005930",
    )
    status = SimpleNamespace(
        remaining_quantity=0,
        filled_quantity=10,
        average_fill_price=Decimal("95000"),
    )
    position = SimpleNamespace(average_entry_price=Decimal("100000"))

    await application._queue_stop_loss_notification(
        intent,  # type: ignore[arg-type]
        status,  # type: ignore[arg-type]
        position,  # type: ignore[arg-type]
        queue,
        {"005930": "삼성전자"},
        repository,  # type: ignore[arg-type]
    )

    assert await queue.get() == (
        "entry-test:005930:SELL:minus5",
        "삼성전자",
        95000,
        Decimal("-5.00"),
    )
