import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from danta.adapters.kis.client import KisApiError
from danta.domain.trading_session import IntentSide, SymbolState
from danta.ports.broker import AccountPosition
from danta.services import paper_trading_application as application_module
from danta.services.capital_allocator import CapitalAllocator
from danta.services.command_store import CommandStatus, StoredCommand
from danta.services.paper_trading_application import PaperTradingApplication
from danta.services.priority_intent_scheduler import PriorityIntentScheduler
from danta.services.trade_notification_outbox import TradeNotificationOutbox
from danta.services.trading_orchestrator import TradingOrchestrator


def test_market_monitor_error_latches_only_during_regular_session() -> None:
    assert not application_module._market_monitor_error_requires_latch(
        datetime(2026, 8, 5, 20, 30, tzinfo=UTC)  # 05:30 KST
    )
    assert application_module._market_monitor_error_requires_latch(
        datetime(2026, 8, 6, 0, 30, tzinfo=UTC)  # 09:30 KST
    )


def test_recovery_capital_includes_held_cost_without_exposing_it_as_cash() -> None:
    positions = [
        AccountPosition("475150", 30, 30, Decimal("54800")),
        AccountPosition("322000", 12, 12, Decimal("129500")),
        AccountPosition("999999", 1, 1, Decimal("500000")),
    ]

    recovered = application_module._recovery_capital_snapshot(
        125_000,
        positions,
        {"475150", "322000"},
    )

    assert recovered == 125_000 + (30 * 54_800) + (12 * 129_500)


def test_recovered_cancelled_buy_advances_attempt_number() -> None:
    assert application_module._next_entry_attempt("entry-1:000660:BUY:A0") == 1
    assert application_module._next_entry_attempt("entry-1:000660:BUY:A12") == 13
    assert application_module._next_entry_attempt("entry-1:000660:SELL") is None


class _GenerationRepository:
    async def latest_generations(self, symbols: list[str]) -> dict[str, int]:
        assert symbols == ["322000", "000660"]
        return {"322000": 0}


async def test_live_mandate_seeds_closed_generation_before_activation() -> None:
    orchestrator = TradingOrchestrator(
        capital_allocator=CapitalAllocator(),
        scheduler=PriorityIntentScheduler(),
    )

    await application_module._seed_latest_closed_generations(
        orchestrator,
        _GenerationRepository(),  # type: ignore[arg-type]
        ["322000", "000660"],
    )

    assert orchestrator.sessions["322000"].generation == 0
    assert orchestrator.sessions["322000"].state is SymbolState.CLOSED
    assert "000660" not in orchestrator.sessions


class _RateLimitedBroker:
    async def daily_order_statuses(self, **_: Any) -> list[object]:
        raise KisApiError("EGW00201", status_code=429)


class _UnexpectedBroker:
    async def daily_order_statuses(self, **_: Any) -> list[object]:
        raise AssertionError("idle runtime must not poll KIS order status")


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


class _LifecycleRepository:
    def __init__(self, closed: set[str]) -> None:
        self.closed = closed
        self.opened_since: datetime | None = None

    async def closed_symbols_since(self, symbols: list[str], *, opened_since: datetime) -> set[str]:
        self.opened_since = opened_since
        return self.closed & set(symbols)


class _LifecycleBroker:
    def __init__(self, statuses: list[object]) -> None:
        self.statuses = statuses
        self.queries: list[str] = []

    async def daily_order_statuses(self, *, trading_date: str) -> list[object]:
        self.queries.append(trading_date)
        return self.statuses


def _stored_command() -> StoredCommand:
    mandate = SimpleNamespace(
        command_id="entry-recovery",
        selections=[
            SimpleNamespace(symbol="005930"),
            SimpleNamespace(symbol="000660"),
        ],
    )
    return StoredCommand(
        mandate=mandate,  # type: ignore[arg-type]
        status=CommandStatus.ACTIVE,
        accepted_at=datetime(2026, 7, 31, 1, tzinfo=UTC),
        source_path=Path("active.json"),
    )


async def test_startup_marks_fully_closed_flat_lifecycle_complete() -> None:
    application = object.__new__(PaperTradingApplication)
    repository = _LifecycleRepository({"005930", "000660"})
    broker = _LifecycleBroker([])

    assert await application._startup_lifecycle_complete(
        active_command=_stored_command(),
        broker=broker,  # type: ignore[arg-type]
        broker_position_symbols=set(),
        repository=repository,  # type: ignore[arg-type]
    )
    assert repository.opened_since == datetime(2026, 7, 31, 1, tzinfo=UTC)
    assert broker.queries


@pytest.mark.parametrize(
    ("closed", "broker_positions", "statuses"),
    [
        ({"005930"}, set(), []),
        ({"005930", "000660"}, {"000660"}, []),
        (
            {"005930", "000660"},
            set(),
            [SimpleNamespace(symbol="005930", remaining_quantity=1)],
        ),
    ],
)
async def test_startup_keeps_incomplete_or_broker_active_lifecycle(
    closed: set[str],
    broker_positions: set[str],
    statuses: list[object],
) -> None:
    application = object.__new__(PaperTradingApplication)

    assert not await application._startup_lifecycle_complete(
        active_command=_stored_command(),
        broker=_LifecycleBroker(statuses),  # type: ignore[arg-type]
        broker_position_symbols=broker_positions,
        repository=_LifecycleRepository(closed),  # type: ignore[arg-type]
    )


async def test_pending_trade_emails_are_replayed_after_restart(tmp_path: Path) -> None:
    application = object.__new__(PaperTradingApplication)
    application.trade_notification_outbox = TradeNotificationOutbox(tmp_path / "notifications")
    application._notified_buy_fill_intents = set()
    application._notified_stop_intents = set()
    application.trade_notification_outbox.enqueue_buy(
        intent_key="entry:005930:BUY:A0",
        correlation_id="entry",
        name="삼성전자",
        price=100_000,
        quantity=10,
    )
    application.trade_notification_outbox.enqueue_exit(
        intent_key="entry:005930:SELL:1",
        correlation_id="entry",
        name="삼성전자",
        price=95_000,
        return_pct=Decimal("-5"),
        cause="HARD_DEFENSE_MINUS_5",
    )
    buy_queue: asyncio.Queue[tuple[str, str, int, int]] = asyncio.Queue()
    exit_queue: asyncio.Queue[tuple[str, str, int, Decimal, str]] = asyncio.Queue()
    repository = _AuditRepository()

    await application._replay_trade_notification_outbox(
        buy_queue,
        exit_queue,
        repository,  # type: ignore[arg-type]
    )

    assert await buy_queue.get() == (
        "entry:005930:BUY:A0",
        "삼성전자",
        100_000,
        10,
    )
    assert await exit_queue.get() == (
        "entry:005930:SELL:1",
        "삼성전자",
        95_000,
        Decimal("-5"),
        "HARD_DEFENSE_MINUS_5",
    )
    assert [event[0] for event in repository.events] == [
        "TRADE_FILL_EMAIL_REPLAYED",
        "TRADE_FILL_EMAIL_REPLAYED",
    ]


async def test_order_status_rate_limit_is_isolated_and_backed_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = object.__new__(PaperTradingApplication)
    application.settings = SimpleNamespace(order_poll_interval_seconds=Decimal("2.0"))
    application.mandate = SimpleNamespace(command_id="entry-test")
    repository = _AuditRepository()

    async def stop_after_backoff(seconds: float) -> None:
        assert seconds == 2.0
        raise asyncio.CancelledError

    monkeypatch.setattr(application_module.asyncio, "sleep", stop_after_backoff)

    with pytest.raises(asyncio.CancelledError):
        await application._poll_orders(
            SimpleNamespace(submitted={"pending": object()}),  # type: ignore[arg-type]
            _RateLimitedBroker(),  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            repository,  # type: ignore[arg-type]
            asyncio.Queue(),
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


async def test_idle_order_reconciler_does_not_call_kis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = object.__new__(PaperTradingApplication)
    application.settings = SimpleNamespace(order_poll_interval_seconds=Decimal("2.0"))
    application.mandate = None

    async def stop_after_idle_sleep(seconds: float) -> None:
        assert seconds == 5.0
        raise asyncio.CancelledError

    monkeypatch.setattr(application_module.asyncio, "sleep", stop_after_idle_sleep)

    with pytest.raises(asyncio.CancelledError):
        await application._poll_orders(
            SimpleNamespace(submitted={}),  # type: ignore[arg-type]
            _UnexpectedBroker(),  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            _AuditRepository(),  # type: ignore[arg-type]
            asyncio.Queue(),
            asyncio.Queue(),
            {},
        )


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
    assert [event[0] for event in repository.events] == ["ENTRY_LIMIT_PRICE_DETERMINED"]


async def test_buy_final_fill_queues_one_notification() -> None:
    application = object.__new__(PaperTradingApplication)
    application.mandate = SimpleNamespace(command_id="entry-test")
    application._notified_buy_fill_intents = set()
    repository = _AuditRepository()
    queue: asyncio.Queue[tuple[str, str, int, int]] = asyncio.Queue()
    intent = SimpleNamespace(
        side=IntentSide.BUY,
        idempotency_key="entry-test:000660:BUY:A0",
        symbol="000660",
    )
    partial = SimpleNamespace(
        remaining_quantity=7,
        filled_quantity=10,
        average_fill_price=Decimal("1329000"),
    )
    complete = SimpleNamespace(
        remaining_quantity=0,
        filled_quantity=17,
        average_fill_price=Decimal("1330000"),
    )

    await application._queue_buy_fill_notification(
        intent,  # type: ignore[arg-type]
        partial,  # type: ignore[arg-type]
        queue,
        {"000660": "SK하이닉스"},
        repository,  # type: ignore[arg-type]
    )
    await application._queue_buy_fill_notification(
        intent,  # type: ignore[arg-type]
        complete,  # type: ignore[arg-type]
        queue,
        {"000660": "SK하이닉스"},
        repository,  # type: ignore[arg-type]
    )
    await application._queue_buy_fill_notification(
        intent,  # type: ignore[arg-type]
        complete,  # type: ignore[arg-type]
        queue,
        {"000660": "SK하이닉스"},
        repository,  # type: ignore[arg-type]
    )

    assert await queue.get() == (
        "entry-test:000660:BUY:A0",
        "SK하이닉스",
        1_330_000,
        17,
    )
    assert queue.empty()
    assert [event[0] for event in repository.events] == ["ENTRY_FILL_EMAIL_QUEUED"]


async def test_hard_stop_final_fill_queues_one_notification() -> None:
    application = object.__new__(PaperTradingApplication)
    application.mandate = SimpleNamespace(command_id="entry-test")
    application._notified_stop_intents = set()
    repository = _AuditRepository()
    queue: asyncio.Queue[tuple[str, str, int, Decimal, str]] = asyncio.Queue()
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

    intent_key, name, price, return_pct, cause = await queue.get()
    assert queue.empty()
    assert intent_key == "entry-test:000660:SELL:1"
    assert name == "SK하이닉스"
    assert price == 1_479_300
    assert return_pct.quantize(Decimal("0.01")) == Decimal("-6.96")
    assert cause == "HARD_STOP_MINUS_7"
    assert [event[0] for event in repository.events] == ["EXIT_FILL_EMAIL_QUEUED"]


async def test_minus_five_defense_final_fill_queues_notification() -> None:
    application = object.__new__(PaperTradingApplication)
    application.mandate = SimpleNamespace(command_id="entry-test")
    application._notified_stop_intents = set()
    repository = _AuditRepository()
    queue: asyncio.Queue[tuple[str, str, int, Decimal, str]] = asyncio.Queue()
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
        "HARD_DEFENSE_MINUS_5",
    )


async def test_adaptive_profit_final_fill_queues_exit_notification() -> None:
    application = object.__new__(PaperTradingApplication)
    application.mandate = SimpleNamespace(command_id="entry-test")
    application._notified_stop_intents = set()
    repository = _AuditRepository()
    queue: asyncio.Queue[tuple[str, str, int, Decimal, str]] = asyncio.Queue()
    intent = SimpleNamespace(
        side=IntentSide.SELL,
        cause="ADAPTIVE_PROFIT_FLOOR",
        idempotency_key="entry-test:000660:SELL:profit",
        symbol="000660",
    )
    status = SimpleNamespace(
        remaining_quantity=0,
        filled_quantity=16,
        average_fill_price=Decimal("1422187"),
    )
    position = SimpleNamespace(average_entry_price=Decimal("1361000"))

    await application._queue_stop_loss_notification(
        intent,  # type: ignore[arg-type]
        status,  # type: ignore[arg-type]
        position,  # type: ignore[arg-type]
        queue,
        {"000660": "SK하이닉스"},
        repository,  # type: ignore[arg-type]
    )

    item = await queue.get()
    assert item[:3] == (
        "entry-test:000660:SELL:profit",
        "SK하이닉스",
        1_422_187,
    )
    assert item[3].quantize(Decimal("0.01")) == Decimal("4.50")
    assert item[4] == "ADAPTIVE_PROFIT_FLOOR"
    assert [event[0] for event in repository.events] == ["EXIT_FILL_EMAIL_QUEUED"]
