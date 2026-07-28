from datetime import UTC, datetime
from decimal import Decimal

from danta.adapters.kis.client import KisOrderStatus
from danta.adapters.kis.realtime import OrderBookTick, TradeTick
from danta.domain.entry import EntryPolicy
from danta.domain.mandate import EntryMandate
from danta.domain.market import MarketRisk
from danta.domain.risk import ExitPolicy
from danta.domain.trading_session import IntentSide, SymbolState
from danta.services.capital_allocator import CapitalAllocator
from danta.services.priority_intent_scheduler import PriorityIntentScheduler
from danta.services.trading_orchestrator import TradingOrchestrator
from danta.services.trading_runtime import ManagedPosition, TradingRuntimeCore


def mandate() -> EntryMandate:
    return EntryMandate.model_validate(
        {
            "report_data_as_of": "2026-07-28T08:00:00+09:00",
            "window_days": 14,
            "authority": "ENTRY_APPROVAL",
            "execution_mode": "USE_LOCKED_ACTIVE_MODE",
            "capital_scope": "KIS_ORDERABLE_CASH",
            "allocation_policy": "USER_DEFINED_ORDERABLE_CASH_PERCENT",
            "total_allocation_pct": "100.0",
            "unallocated_cash_pct": "0.0",
            "selected_symbol_count": 1,
            "entry_trigger": "LAST_PRICE_LTE_TARGET",
            "validity_policy": "UNTIL_FILLED_OR_USER_CANCELLED",
            "partial_fill_policy": (
                "PROTECT_FILLED_CANCEL_REMAINDER_ON_SAFETY_DETERIORATION"
            ),
            "duplicate_guard": "INTERNAL_ON_INGEST",
            "hard_stop_pct": "-7.0",
            "profit_policy": "ACTIVE_VERSIONED_LOCAL_ENGINE",
            "selections": [
                {
                    "rank": 150,
                    "symbol": "005930",
                    "name": "삼성전자",
                    "entry_target_price_krw": 100000,
                    "entry_price_source": "USER_EDITED",
                    "allocation_pct": "100.0",
                    "ai_grade": "추천",
                    "box_low": "95000",
                    "box_high": "110000",
                }
            ],
            "request": "paper entry",
        }
    )


def entry_policy() -> EntryPolicy:
    return EntryPolicy(
        version="entry-test-v1",
        approved=True,
        max_snapshot_age_seconds=10,
        sell_pressure_block=Decimal("0.8"),
        stabilization_required=Decimal("0"),
        buy_recovery_required=Decimal("0"),
        max_spread_bps=Decimal("100"),
    )


def exit_policy() -> ExitPolicy:
    return ExitPolicy(
        version="exit-test-v1",
        approved=True,
        early_loss_pct=Decimal("-3"),
        strong_loss_pct=Decimal("-5"),
        early_defense_score=Decimal("0.7"),
        strong_sell_pressure=Decimal("0.7"),
        panic_market_stress=Decimal("0.7"),
        profit_arm_pct=Decimal("5"),
        profit_giveback_pct=Decimal("1.5"),
        profit_weakness_score=Decimal("0.6"),
        max_holding_minutes=1440,
    )


def trade(price: int, now: datetime) -> TradeTick:
    return TradeTick(
        symbol="005930",
        observed_at=now,
        price=price,
        best_ask=price,
        best_bid=price - 10,
        trade_volume=10,
        accumulated_value=1000000,
        sell_trade_count=10,
        buy_trade_count=20,
        trade_strength=Decimal("120"),
        ask_quantity=10,
        bid_quantity=20,
        total_ask_quantity=100,
        total_bid_quantity=200,
    )


def orderbook(price: int, now: datetime) -> OrderBookTick:
    return OrderBookTick(
        symbol="005930",
        observed_at=now,
        best_ask=price,
        best_bid=price - 10,
        ask_prices=tuple(price + index * 10 for index in range(10)),
        bid_prices=tuple(price - 10 - index * 10 for index in range(10)),
        ask_quantities=(10,) * 10,
        bid_quantities=(20,) * 10,
        total_ask_quantity=100,
        total_bid_quantity=200,
    )


async def test_runtime_enters_tracks_fill_and_hard_stops() -> None:
    orchestrator = TradingOrchestrator(
        capital_allocator=CapitalAllocator(),
        scheduler=PriorityIntentScheduler(),
    )
    await orchestrator.reconcile_complete(safe_for_new_entries=True)
    core = TradingRuntimeCore(
        orchestrator=orchestrator,
        entry_policy=entry_policy(),
        exit_policy=exit_policy(),
    )
    await core.activate_mandate(mandate(), orderable_cash=1000000)
    now = datetime.now(UTC)
    assert await core.process_event(orderbook(99000, now), now=now) is None
    buy = await core.process_event(trade(99000, now), now=now)
    assert buy is not None
    assert buy.side is IntentSide.BUY
    core.record_submission(
        buy,
        type(
            "Execution",
            (),
            {
                "idempotency_key": buy.idempotency_key,
                "broker_order_no": "100",
                "status": "SUBMITTED",
            },
        )(),
    )
    delta = core.apply_order_status(
        KisOrderStatus(
            broker_order_no="100",
            original_order_no="",
            symbol="005930",
            side="BUY",
            ordered_quantity=10,
            filled_quantity=10,
            remaining_quantity=0,
            order_price=99000,
            average_fill_price=Decimal("99000"),
            order_time="090001",
            branch_no="1",
        ),
        observed_at=now,
    )
    assert delta == 10
    assert core.positions["005930"].quantity == 10
    sell = await core.process_event(trade(92000, now), now=now)
    assert sell is not None
    assert sell.side is IntentSide.SELL
    assert sell.cause == "HARD_STOP_MINUS_7"


async def test_box_break_does_not_cancel_pending_entry_approval() -> None:
    orchestrator = TradingOrchestrator(
        capital_allocator=CapitalAllocator(),
        scheduler=PriorityIntentScheduler(),
    )
    await orchestrator.reconcile_complete(safe_for_new_entries=True)
    core = TradingRuntimeCore(
        orchestrator=orchestrator,
        entry_policy=entry_policy(),
        exit_policy=exit_policy(),
    )
    await core.activate_mandate(mandate(), orderable_cash=1000000)
    now = datetime.now(UTC)
    buy = await core.process_event(trade(99000, now), now=now)
    assert buy is not None
    core.record_submission(
        buy,
        type(
            "Execution",
            (),
            {
                "idempotency_key": buy.idempotency_key,
                "broker_order_no": "200",
                "status": "SUBMITTED",
            },
        )(),
    )
    core.invalidate_box("005930")
    status = KisOrderStatus(
        broker_order_no="200",
        original_order_no="",
        symbol="005930",
        side="BUY",
        ordered_quantity=10,
        filled_quantity=0,
        remaining_quantity=10,
        order_price=99000,
        average_fill_price=Decimal("0"),
        order_time="090001",
        branch_no="1",
    )
    assert not core.cancellation_required(status)


async def test_deteriorated_pending_buy_cancels_and_rearms_at_lower_price() -> None:
    orchestrator = TradingOrchestrator(
        capital_allocator=CapitalAllocator(),
        scheduler=PriorityIntentScheduler(),
    )
    await orchestrator.reconcile_complete(safe_for_new_entries=True)
    core = TradingRuntimeCore(
        orchestrator=orchestrator,
        entry_policy=entry_policy(),
        exit_policy=exit_policy(),
    )
    await core.activate_mandate(mandate(), orderable_cash=1000000)
    now = datetime.now(UTC)
    first = await core.process_event(trade(99000, now), now=now)
    assert first is not None
    assert first.idempotency_key.endswith(":A0")
    core.record_submission(
        first,
        type(
            "Execution",
            (),
            {
                "idempotency_key": first.idempotency_key,
                "broker_order_no": "300",
                "status": "SUBMITTED",
            },
        )(),
    )

    core.set_market_guard(MarketRisk.RISK_OFF, stress_score=Decimal("0.9"))
    assert await core.process_event(trade(98000, now), now=now) is None
    open_status = KisOrderStatus(
        broker_order_no="300",
        original_order_no="",
        symbol="005930",
        side="BUY",
        ordered_quantity=10,
        filled_quantity=0,
        remaining_quantity=10,
        order_price=99000,
        average_fill_price=Decimal("0"),
        order_time="090001",
        branch_no="1",
    )
    assert core.cancellation_required(open_status)
    core.record_cancellation_requested("300")
    cancelled_status = KisOrderStatus(
        broker_order_no="300",
        original_order_no="",
        symbol="005930",
        side="BUY",
        ordered_quantity=10,
        filled_quantity=0,
        remaining_quantity=0,
        order_price=99000,
        average_fill_price=Decimal("0"),
        order_time="090001",
        branch_no="1",
    )
    assert await core.finalize_buy_cancellation(cancelled_status)
    assert orchestrator.sessions["005930"].state is SymbolState.WATCHING_ENTRY

    core.set_market_guard(MarketRisk.NORMAL, stress_score=Decimal("0"))
    second = await core.process_event(trade(97000, now), now=now)
    assert second is not None
    assert second.limit_price == 97000
    assert second.idempotency_key.endswith(":A1")
    assert second.idempotency_key != first.idempotency_key


async def test_partial_fill_cancel_keeps_position_and_does_not_rearm() -> None:
    orchestrator = TradingOrchestrator(
        capital_allocator=CapitalAllocator(),
        scheduler=PriorityIntentScheduler(),
    )
    await orchestrator.reconcile_complete(safe_for_new_entries=True)
    core = TradingRuntimeCore(
        orchestrator=orchestrator,
        entry_policy=entry_policy(),
        exit_policy=exit_policy(),
    )
    await core.activate_mandate(mandate(), orderable_cash=1000000)
    now = datetime.now(UTC)
    buy = await core.process_event(trade(99000, now), now=now)
    assert buy is not None
    core.record_submission(
        buy,
        type(
            "Execution",
            (),
            {
                "idempotency_key": buy.idempotency_key,
                "broker_order_no": "400",
                "status": "SUBMITTED",
            },
        )(),
    )
    partial = KisOrderStatus(
        broker_order_no="400",
        original_order_no="",
        symbol="005930",
        side="BUY",
        ordered_quantity=10,
        filled_quantity=2,
        remaining_quantity=8,
        order_price=99000,
        average_fill_price=Decimal("99000"),
        order_time="090001",
        branch_no="1",
    )
    assert core.apply_order_status(partial, observed_at=now) == 2
    core.request_buy_reprice("005930", reason="SELL_PRESSURE_STRONG")
    assert core.cancellation_required(partial)
    core.record_cancellation_requested("400")
    cancelled = KisOrderStatus(
        broker_order_no="400",
        original_order_no="",
        symbol="005930",
        side="BUY",
        ordered_quantity=10,
        filled_quantity=2,
        remaining_quantity=0,
        order_price=99000,
        average_fill_price=Decimal("99000"),
        order_time="090001",
        branch_no="1",
    )
    assert await core.finalize_buy_cancellation(cancelled)
    assert core.positions["005930"].quantity == 2
    assert orchestrator.sessions["005930"].state is SymbolState.POSITION_OPEN


async def test_rest_watchdog_hard_stops_without_realtime_tick() -> None:
    orchestrator = TradingOrchestrator(
        capital_allocator=CapitalAllocator(),
        scheduler=PriorityIntentScheduler(),
    )
    await orchestrator.reconcile_complete(safe_for_new_entries=True)
    core = TradingRuntimeCore(
        orchestrator=orchestrator,
        entry_policy=entry_policy(),
        exit_policy=exit_policy(),
    )
    await core.activate_mandate(mandate(), orderable_cash=1000000)
    now = datetime.now(UTC)
    session = orchestrator.sessions["005930"]
    session.state = type(session.state).POSITION_OPEN
    session.quantity = 3
    session.sellable_quantity = 3
    core.positions["005930"] = ManagedPosition(
        symbol="005930",
        generation=session.generation,
        quantity=3,
        sellable_quantity=3,
        average_entry_price=Decimal("100000"),
        opened_at=now,
    )
    intent = await core.process_watchdog_price(
        symbol="005930", price=93000, observed_at=now
    )
    assert intent is not None
    assert intent.cause == "HARD_STOP_MINUS_7"
