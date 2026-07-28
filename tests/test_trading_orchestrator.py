from datetime import UTC, datetime
from decimal import Decimal

from danta.domain.entry import EntryAction, EntryDecision
from danta.domain.mandate import EntryMandate, EntrySelection
from danta.domain.risk import ExitAction, ExitDecision, ExitUrgency
from danta.domain.trading_session import IntentPriority, IntentSide, SymbolState
from danta.services.capital_allocator import CapitalAllocator
from danta.services.priority_intent_scheduler import PriorityIntentScheduler
from danta.services.trading_orchestrator import TradingOrchestrator


def _mandate() -> EntryMandate:
    selections = [
        EntrySelection(
            rank=1,
            symbol="005930",
            name="삼성전자",
            entry_target_price_krw=70000,
            entry_price_source="USER_EDITED",
            allocation_pct=Decimal("50.0"),
            ai_grade="추천",
            box_low=Decimal("68000"),
            box_high=Decimal("78000"),
        ),
        EntrySelection(
            rank=2,
            symbol="000660",
            name="SK하이닉스",
            entry_target_price_krw=180000,
            entry_price_source="USER_EDITED",
            allocation_pct=Decimal("50.0"),
            ai_grade="추천",
            box_low=Decimal("170000"),
            box_high=Decimal("195000"),
        ),
    ]
    return EntryMandate(
        report_data_as_of=datetime.now(UTC),
        window_days=14,
        authority="ENTRY_APPROVAL",
        execution_mode="USE_LOCKED_ACTIVE_MODE",
        capital_scope="KIS_ORDERABLE_CASH",
        allocation_policy="USER_DEFINED_ORDERABLE_CASH_PERCENT",
        total_allocation_pct=Decimal("100.0"),
        unallocated_cash_pct=Decimal("0.0"),
        selected_symbol_count=2,
        entry_trigger="LAST_PRICE_LTE_TARGET",
        validity_policy="UNTIL_FILLED_OR_USER_CANCELLED",
        partial_fill_policy="PROTECT_FILLED_CANCEL_REMAINDER_ON_SAFETY_DETERIORATION",
        duplicate_guard="INTERNAL_ON_INGEST",
        hard_stop_pct=Decimal("-7.0"),
        profit_policy="ACTIVE_VERSIONED_LOCAL_ENGINE",
        selections=selections,
        request="test",
    )


async def test_multi_symbol_entry_and_hard_stop_priority() -> None:
    scheduler = PriorityIntentScheduler()
    orchestrator = TradingOrchestrator(
        capital_allocator=CapitalAllocator(),
        scheduler=scheduler,
    )
    mandate = _mandate()
    await orchestrator.register_mandate(mandate, orderable_cash=1_000_000)
    await orchestrator.reconcile_complete(safe_for_new_entries=True)
    entry = EntryDecision(
        symbol="005930",
        action=EntryAction.SUBMIT_LIMIT_BUY,
        policy_version="entry-v1",
        limit_price=69000,
        reason_codes=("READY",),
    )
    entry_intent = await orchestrator.handle_entry_decision(
        mandate_id=mandate.command_id,
        decision=entry,
        created_at=datetime.now(UTC),
    )
    assert entry_intent is not None
    orchestrator.record_fill(
        symbol="005930",
        side=IntentSide.BUY,
        filled_quantity=entry_intent.quantity,
        remaining_quantity=0,
    )
    assert orchestrator.sessions["005930"].state is SymbolState.POSITION_OPEN

    exit_intent = await orchestrator.handle_exit_decision(
        ExitDecision(
            symbol="005930",
            generation=0,
            action=ExitAction.SELL_MARKET,
            urgency=ExitUrgency.HARD_STOP,
            quantity=entry_intent.quantity,
            policy_version="hard-stop-v1",
            reason_codes=("HARD_STOP_MINUS_7",),
        ),
        created_at=datetime.now(UTC),
    )
    assert exit_intent is not None
    assert (await scheduler.get()).priority is IntentPriority.HARD_STOP_EXIT


async def test_entry_is_blocked_until_reconciliation_completes() -> None:
    orchestrator = TradingOrchestrator(
        capital_allocator=CapitalAllocator(),
        scheduler=PriorityIntentScheduler(),
    )
    mandate = _mandate()
    await orchestrator.register_mandate(mandate, orderable_cash=1_000_000)
    decision = EntryDecision(
        "005930", EntryAction.SUBMIT_LIMIT_BUY, "entry-v1", 69000, ("READY",)
    )
    assert (
        await orchestrator.handle_entry_decision(
            mandate_id=mandate.command_id,
            decision=decision,
            created_at=datetime.now(UTC),
        )
        is None
    )
