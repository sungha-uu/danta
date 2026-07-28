from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from danta.adapters.kis.client import KisOrderStatus
from danta.adapters.kis.realtime import RealtimeEvent
from danta.domain.entry import EntryPolicy, evaluate_entry
from danta.domain.mandate import EntryMandate, PlannedEntry
from danta.domain.market import MarketRisk
from danta.domain.risk import ExitPolicy, PositionRiskSnapshot, evaluate_exit
from danta.domain.trading_session import IntentSide, OrderIntent, SymbolState
from danta.services.market_signal import RollingMarketSignal
from danta.services.order_manager import (
    OrderExecution,
    OrderManager,
    UnknownOrderOutcome,
)
from danta.services.trading_orchestrator import TradingOrchestrator


@dataclass(slots=True)
class ManagedPosition:
    symbol: str
    generation: int
    quantity: int
    sellable_quantity: int
    average_entry_price: Decimal
    opened_at: datetime
    peak_return_pct: Decimal = Decimal("0")

    def apply_buy_fill(self, *, quantity: int, price: Decimal) -> None:
        if quantity <= 0 or price <= 0:
            raise ValueError("fill must have positive quantity and price")
        old_value = self.average_entry_price * self.quantity
        self.quantity += quantity
        self.sellable_quantity += quantity
        self.average_entry_price = (old_value + price * quantity) / self.quantity

    def apply_sell_fill(self, *, quantity: int) -> None:
        if quantity <= 0 or quantity > self.quantity:
            raise ValueError("sell fill quantity is invalid")
        self.quantity -= quantity
        self.sellable_quantity = max(0, self.sellable_quantity - quantity)


@dataclass(slots=True)
class SubmittedOrder:
    intent: OrderIntent
    execution: OrderExecution
    cumulative_filled: int = 0
    cumulative_fill_value: Decimal = Decimal("0")
    last_fill_value: int = 0


class TradingRuntimeCore:
    """Deterministic runtime core; network and process lifecycle are external."""

    def __init__(
        self,
        *,
        orchestrator: TradingOrchestrator,
        entry_policy: EntryPolicy,
        exit_policy: ExitPolicy,
    ) -> None:
        self.orchestrator = orchestrator
        self.entry_policy = entry_policy
        self.exit_policy = exit_policy
        self.mandate: EntryMandate | None = None
        self.plans: dict[str, PlannedEntry] = {}
        self.signals: dict[str, RollingMarketSignal] = {}
        self.positions: dict[str, ManagedPosition] = {}
        self.submitted: dict[str, SubmittedOrder] = {}
        self.order_number_to_key: dict[str, str] = {}
        self.cancel_requested: set[str] = set()
        self.cancel_completed: set[str] = set()
        self.pending_buy_cancel_reason: dict[str, str] = {}
        self.entry_attempts: dict[str, int] = {}
        self.box_valid: dict[str, bool] = {}
        self.market_risk = MarketRisk.NORMAL
        self.market_stress_score = Decimal("0")

    async def activate_mandate(
        self,
        mandate: EntryMandate,
        *,
        orderable_cash: int,
    ) -> list[PlannedEntry]:
        plans = await self.orchestrator.register_mandate(
            mandate, orderable_cash=orderable_cash
        )
        self.mandate = mandate
        self.plans = {plan.symbol: plan for plan in plans}
        self.signals = {
            selection.symbol: RollingMarketSignal(selection.symbol)
            for selection in mandate.selections
        }
        self.box_valid = {selection.symbol: True for selection in mandate.selections}
        self.entry_attempts = {selection.symbol: 0 for selection in mandate.selections}
        return plans

    def set_market_guard(
        self, risk: MarketRisk, *, stress_score: Decimal
    ) -> None:
        if stress_score < 0 or stress_score > 1:
            raise ValueError("stress_score must be between 0 and 1")
        self.market_risk = risk
        self.market_stress_score = stress_score

    def restore_position(
        self,
        position: ManagedPosition,
    ) -> None:
        session = self.orchestrator.sessions.get(position.symbol)
        if session is None:
            raise ValueError("orchestrator session must be restored first")
        if session.generation != position.generation:
            raise ValueError("restored position generation does not match session")
        self.positions[position.symbol] = position
        self.signals.setdefault(position.symbol, RollingMarketSignal(position.symbol))
        self.box_valid.setdefault(position.symbol, True)

    def invalidate_box(self, symbol: str) -> None:
        if symbol not in self.box_valid:
            raise ValueError("symbol is outside the active mandate")
        self.box_valid[symbol] = False

    def cancellation_required(self, status: KisOrderStatus) -> bool:
        key = self.order_number_to_key.get(status.broker_order_no)
        if key is None or status.broker_order_no in self.cancel_requested:
            return False
        intent = self.submitted[key].intent
        return (
            intent.side is IntentSide.BUY
            and intent.symbol in self.pending_buy_cancel_reason
            and status.remaining_quantity > 0
        )

    def record_cancellation_requested(self, broker_order_no: str) -> None:
        self.cancel_requested.add(broker_order_no)

    def request_buy_reprice(self, symbol: str, *, reason: str) -> None:
        if symbol not in self.plans:
            raise ValueError("symbol is outside the active mandate")
        if not reason:
            raise ValueError("buy reprice reason is required")
        self.pending_buy_cancel_reason[symbol] = reason

    async def finalize_buy_cancellation(self, status: KisOrderStatus) -> bool:
        if (
            status.broker_order_no not in self.cancel_requested
            or status.broker_order_no in self.cancel_completed
            or status.remaining_quantity > 0
            or status.filled_quantity >= status.ordered_quantity
        ):
            return False
        key = self.order_number_to_key.get(status.broker_order_no)
        if key is None:
            return False
        submitted = self.submitted[key]
        intent = submitted.intent
        if intent.side is not IntentSide.BUY:
            return False
        await self.orchestrator.capital_allocator.release(
            f"{intent.idempotency_key}:CAPITAL"
        )
        await self.orchestrator.scheduler.forget(intent.idempotency_key)
        session = self.orchestrator.sessions[intent.symbol]
        session.active_order_key = None
        self.cancel_completed.add(status.broker_order_no)
        self.pending_buy_cancel_reason.pop(intent.symbol, None)
        if self.positions.get(intent.symbol) is not None:
            session.state = SymbolState.POSITION_OPEN
        else:
            self.entry_attempts[intent.symbol] = (
                self.entry_attempts.get(intent.symbol, 0) + 1
            )
            session.state = SymbolState.WATCHING_ENTRY
        return True

    async def process_event(
        self, event: RealtimeEvent, *, now: datetime | None = None
    ) -> OrderIntent | None:
        signal = self.signals.get(event.symbol)
        if signal is None:
            return None
        signal.update(event)
        if not signal.ready:
            return None
        observed_now = now or datetime.now(UTC)
        snapshot = signal.snapshot(
            now=observed_now,
            market_risk=self.market_risk,
            market_stress_score=self.market_stress_score,
            box_valid=self.box_valid[event.symbol],
            data_fresh=True,
        )
        session = self.orchestrator.sessions[event.symbol]
        if session.state in {
            SymbolState.WATCHING_ENTRY,
            SymbolState.BUY_PENDING,
            SymbolState.PARTIALLY_FILLED,
        }:
            mandate = self._require_mandate()
            entry_decision = evaluate_entry(
                snapshot,
                maximum_price=self.plans[event.symbol].target_price,
                policy=self.entry_policy,
                snapshot_is_fresh=snapshot.is_fresh(
                    now=observed_now,
                    max_age_seconds=self.entry_policy.max_snapshot_age_seconds,
                ),
            )
            if session.state is SymbolState.WATCHING_ENTRY:
                return await self.orchestrator.handle_entry_decision(
                    mandate_id=mandate.command_id,
                    decision=entry_decision,
                    created_at=observed_now,
                    attempt=self.entry_attempts.get(event.symbol, 0),
                )
            if entry_decision.action.value != "SUBMIT_LIMIT_BUY":
                reason = (
                    entry_decision.reason_codes[0]
                    if entry_decision.reason_codes
                    else "ENTRY_CONDITION_DETERIORATED"
                )
                self.request_buy_reprice(event.symbol, reason=reason)
        position = self.positions.get(event.symbol)
        if position is None or position.quantity <= 0:
            return None
        executable_price = min(
            snapshot.last_price,
            snapshot.best_bid if snapshot.best_bid is not None else snapshot.last_price,
        )
        current_return = (
            (Decimal(executable_price) - position.average_entry_price)
            / position.average_entry_price
            * Decimal("100")
        )
        position.peak_return_pct = max(position.peak_return_pct, current_return)
        held_minutes = max(
            0, int((observed_now - position.opened_at).total_seconds() // 60)
        )
        exit_decision = evaluate_exit(
            PositionRiskSnapshot(
                symbol=position.symbol,
                generation=position.generation,
                average_entry_price=position.average_entry_price,
                quantity=position.quantity,
                sellable_quantity=position.sellable_quantity,
                last_price=snapshot.last_price,
                best_bid=snapshot.best_bid,
                broker_return_pct=None,
                peak_return_pct=position.peak_return_pct,
                held_minutes=held_minutes,
                sell_pressure_score=snapshot.sell_pressure_score,
                weakness_score=snapshot.weakness_score,
                market_stress_score=snapshot.market_stress_score,
                market_risk=snapshot.market_risk,
                box_valid=snapshot.box_valid,
                data_fresh=snapshot.data_fresh,
                observed_at=snapshot.observed_at,
            ),
            policy=self.exit_policy,
        )
        return await self.orchestrator.handle_exit_decision(
            exit_decision, created_at=observed_now
        )

    async def process_watchdog_price(
        self,
        *,
        symbol: str,
        price: int,
        observed_at: datetime,
    ) -> OrderIntent | None:
        """Independent REST hard-stop path used when WebSocket health is unknown."""
        position = self.positions.get(symbol)
        if position is None:
            return None
        decision = evaluate_exit(
            PositionRiskSnapshot(
                symbol=symbol,
                generation=position.generation,
                average_entry_price=position.average_entry_price,
                quantity=position.quantity,
                sellable_quantity=position.sellable_quantity,
                last_price=price,
                best_bid=None,
                broker_return_pct=None,
                peak_return_pct=position.peak_return_pct,
                held_minutes=max(
                    0,
                    int((observed_at - position.opened_at).total_seconds() // 60),
                ),
                sell_pressure_score=Decimal("0"),
                weakness_score=Decimal("0"),
                market_stress_score=self.market_stress_score,
                market_risk=self.market_risk,
                box_valid=True,
                data_fresh=True,
                observed_at=observed_at,
            ),
            policy=self.exit_policy,
        )
        if "HARD_STOP_MINUS_7" not in decision.reason_codes:
            return None
        return await self.orchestrator.handle_exit_decision(
            decision, created_at=observed_at
        )

    def record_submission(
        self, intent: OrderIntent, execution: OrderExecution
    ) -> None:
        existing = self.order_number_to_key.get(execution.broker_order_no)
        if existing is not None and existing != intent.idempotency_key:
            raise RuntimeError("broker order number is mapped to multiple intents")
        self.submitted[intent.idempotency_key] = SubmittedOrder(intent, execution)
        self.order_number_to_key[execution.broker_order_no] = intent.idempotency_key

    def apply_order_status(self, status: KisOrderStatus, *, observed_at: datetime) -> int:
        key = self.order_number_to_key.get(status.broker_order_no)
        if key is None:
            return 0
        submitted = self.submitted[key]
        if status.filled_quantity < submitted.cumulative_filled:
            raise RuntimeError("broker cumulative fill quantity moved backward")
        delta = status.filled_quantity - submitted.cumulative_filled
        if delta == 0:
            return 0
        submitted.cumulative_filled = status.filled_quantity
        intent = submitted.intent
        fill_price = status.average_fill_price
        if fill_price <= 0:
            raise RuntimeError("filled order did not include an average fill price")
        cumulative_value = fill_price * status.filled_quantity
        incremental_value = cumulative_value - submitted.cumulative_fill_value
        if incremental_value <= 0:
            raise RuntimeError("broker cumulative fill value did not increase")
        submitted.cumulative_fill_value = cumulative_value
        submitted.last_fill_value = int(incremental_value)
        incremental_price = incremental_value / delta
        if intent.side is IntentSide.BUY:
            position = self.positions.get(intent.symbol)
            if position is None:
                position = ManagedPosition(
                    symbol=intent.symbol,
                    generation=intent.generation,
                    quantity=0,
                    sellable_quantity=0,
                    average_entry_price=Decimal("0"),
                    opened_at=observed_at,
                )
                self.positions[intent.symbol] = position
            position.apply_buy_fill(quantity=delta, price=incremental_price)
        elif intent.side is IntentSide.SELL:
            position = self.positions[intent.symbol]
            position.apply_sell_fill(quantity=delta)
            if position.quantity == 0:
                del self.positions[intent.symbol]
        self.orchestrator.record_fill(
            symbol=intent.symbol,
            side=intent.side,
            filled_quantity=delta,
            remaining_quantity=status.remaining_quantity,
        )
        return delta

    def _require_mandate(self) -> EntryMandate:
        if self.mandate is None:
            raise RuntimeError("entry mandate is not active")
        return self.mandate


OrderPumpErrorSink = Callable[[OrderIntent, BaseException], Awaitable[None]]


class OrderPump:
    def __init__(
        self,
        *,
        core: TradingRuntimeCore,
        manager: OrderManager,
        on_error: OrderPumpErrorSink | None = None,
    ) -> None:
        self.core = core
        self.manager = manager
        self.on_error = on_error

    async def run(self) -> None:
        scheduler = self.core.orchestrator.scheduler
        while True:
            intent = await scheduler.get()
            try:
                try:
                    execution = await self.manager.execute(intent)
                    self.core.record_submission(intent, execution)
                except Exception as exc:
                    await self.core.orchestrator.record_order_failure(
                        intent,
                        outcome_unknown=isinstance(exc, UnknownOrderOutcome),
                    )
                    if self.on_error is not None:
                        await self.on_error(intent, exc)
            finally:
                scheduler.task_done()

    async def run_until_cancelled(self) -> None:
        try:
            await self.run()
        except asyncio.CancelledError:
            raise
