from __future__ import annotations

from datetime import datetime

from danta.domain.entry import EntryAction, EntryDecision
from danta.domain.mandate import EntryMandate, PlannedEntry, plan_entries
from danta.domain.risk import ExitAction, ExitDecision, ExitUrgency
from danta.domain.trading_session import (
    IntentPriority,
    IntentSide,
    OrchestratorState,
    OrderIntent,
    SymbolSession,
    SymbolState,
)
from danta.services.capital_allocator import CapitalAllocator
from danta.services.priority_intent_scheduler import PriorityIntentScheduler


class TradingOrchestrator:
    def __init__(
        self,
        *,
        capital_allocator: CapitalAllocator,
        scheduler: PriorityIntentScheduler,
        max_approved_symbols: int = 3,
    ) -> None:
        if max_approved_symbols <= 0:
            raise ValueError("max_approved_symbols must be positive")
        self.capital_allocator = capital_allocator
        self.scheduler = scheduler
        self.max_approved_symbols = max_approved_symbols
        self.state = OrchestratorState.BOOTING
        self.sessions: dict[str, SymbolSession] = {}
        self._mandates: dict[str, EntryMandate] = {}
        self._plans: dict[tuple[str, str], PlannedEntry] = {}

    async def reconcile_complete(self, *, safe_for_new_entries: bool) -> None:
        self.state = (
            OrchestratorState.RUNNING
            if safe_for_new_entries
            else OrchestratorState.ENTRY_BLOCKED
        )

    async def register_mandate(
        self,
        mandate: EntryMandate,
        *,
        orderable_cash: int,
    ) -> list[PlannedEntry]:
        if mandate.selected_symbol_count > self.max_approved_symbols:
            raise ValueError("mandate exceeds orchestrator symbol limit")
        plans = plan_entries(mandate, orderable_cash=orderable_cash)
        allocations = {
            selection.symbol: selection.allocation_pct for selection in mandate.selections
        }
        await self.capital_allocator.register_mandate(
            mandate_id=mandate.command_id,
            orderable_cash=orderable_cash,
            allocations=allocations,
        )
        self._mandates[mandate.command_id] = mandate
        for plan in plans:
            key = (mandate.command_id, plan.symbol)
            self._plans[key] = plan
            session = self.sessions.get(plan.symbol)
            if session is not None and session.state not in {
                SymbolState.IDLE,
                SymbolState.CLOSED,
                SymbolState.INVALIDATED,
            }:
                raise ValueError(f"symbol already has an active session: {plan.symbol}")
            generation = 0 if session is None else session.generation + 1
            self.sessions[plan.symbol] = SymbolSession(
                symbol=plan.symbol,
                generation=generation,
                state=SymbolState.WATCHING_ENTRY,
            )
        return plans

    async def handle_entry_decision(
        self,
        *,
        mandate_id: str,
        decision: EntryDecision,
        created_at: datetime,
        attempt: int = 0,
    ) -> OrderIntent | None:
        if attempt < 0:
            raise ValueError("entry attempt must not be negative")
        if self.state is not OrchestratorState.RUNNING:
            return None
        key = (mandate_id, decision.symbol)
        plan = self._plans.get(key)
        session = self.sessions.get(decision.symbol)
        if plan is None or session is None:
            raise ValueError("entry decision is outside an active mandate")
        if session.state is not SymbolState.WATCHING_ENTRY:
            return None
        if decision.action is EntryAction.INVALIDATE_MANDATE:
            session.state = SymbolState.INVALIDATED
            return None
        if decision.action is not EntryAction.SUBMIT_LIMIT_BUY:
            return None
        if decision.limit_price is None or decision.limit_price > plan.target_price:
            raise ValueError("entry decision violates maximum buy price")
        attempt_key = f"{plan.idempotency_key}:A{attempt}"
        reservation_id = f"{attempt_key}:CAPITAL"
        amount = plan.quantity * decision.limit_price
        await self.capital_allocator.reserve(
            mandate_id=mandate_id,
            symbol=plan.symbol,
            amount=amount,
            reservation_id=reservation_id,
        )
        intent = OrderIntent(
            idempotency_key=attempt_key,
            symbol=plan.symbol,
            generation=session.generation,
            side=IntentSide.BUY,
            priority=IntentPriority.ENTRY,
            quantity=plan.quantity,
            order_type="LIMIT",
            limit_price=decision.limit_price,
            cause="ENTRY_CONFIRMED",
            policy_version=decision.policy_version,
            created_at=created_at,
            approval_id=mandate_id,
        )
        if not await self.scheduler.put(intent):
            await self.capital_allocator.release(reservation_id)
            return None
        session.state = SymbolState.BUY_PENDING
        session.active_order_key = intent.idempotency_key
        return intent

    async def handle_exit_decision(
        self,
        decision: ExitDecision,
        *,
        created_at: datetime,
    ) -> OrderIntent | None:
        if decision.action is not ExitAction.SELL_MARKET:
            return None
        session = self.sessions.get(decision.symbol)
        if session is None or session.generation != decision.generation:
            raise ValueError("exit decision does not match an active position generation")
        if decision.quantity <= 0:
            return None
        priority = {
            ExitUrgency.HARD_STOP: IntentPriority.HARD_STOP_EXIT,
            ExitUrgency.PROTECTIVE: IntentPriority.PROTECTIVE_EXIT,
            ExitUrgency.NORMAL: IntentPriority.PROFIT_OR_TIME_EXIT,
            ExitUrgency.NONE: IntentPriority.PROFIT_OR_TIME_EXIT,
        }[decision.urgency]
        cause = decision.reason_codes[0] if decision.reason_codes else "EXIT"
        intent = OrderIntent(
            idempotency_key=(
                f"{decision.symbol}:{decision.generation}:SELL:{cause}:{decision.policy_version}"
            ),
            symbol=decision.symbol,
            generation=decision.generation,
            side=IntentSide.SELL,
            priority=priority,
            quantity=decision.quantity,
            order_type="MARKET",
            limit_price=None,
            cause=cause,
            policy_version=decision.policy_version,
            created_at=created_at,
        )
        if not await self.scheduler.put(intent):
            return None
        session.state = SymbolState.SELL_PENDING
        session.active_order_key = intent.idempotency_key
        return intent

    def record_fill(
        self,
        *,
        symbol: str,
        side: IntentSide,
        filled_quantity: int,
        remaining_quantity: int,
    ) -> None:
        if filled_quantity < 0 or remaining_quantity < 0:
            raise ValueError("fill quantities must not be negative")
        session = self.sessions[symbol]
        if side is IntentSide.BUY:
            session.quantity += filled_quantity
            session.sellable_quantity += filled_quantity
            session.state = (
                SymbolState.POSITION_OPEN
                if remaining_quantity == 0
                else SymbolState.PARTIALLY_FILLED
            )
        elif side is IntentSide.SELL:
            session.quantity = max(0, session.quantity - filled_quantity)
            session.sellable_quantity = max(
                0, session.sellable_quantity - filled_quantity
            )
            session.state = (
                SymbolState.CLOSED
                if session.quantity == 0 and remaining_quantity == 0
                else SymbolState.SELL_PENDING
            )

    async def record_order_failure(
        self, intent: OrderIntent, *, outcome_unknown: bool
    ) -> None:
        session = self.sessions[intent.symbol]
        if outcome_unknown:
            session.state = SymbolState.QUARANTINED
            self.state = OrchestratorState.ENTRY_BLOCKED
            return
        session.active_order_key = None
        if intent.side is IntentSide.BUY:
            session.state = SymbolState.WATCHING_ENTRY
            await self.capital_allocator.release(
                f"{intent.idempotency_key}:CAPITAL"
            )
        elif intent.side is IntentSide.SELL:
            session.state = SymbolState.POSITION_OPEN
        await self.scheduler.forget(intent.idempotency_key)
