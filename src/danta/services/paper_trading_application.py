from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text

from danta.adapters.kis.client import KisApiError, KisClient, KisOrderStatus
from danta.adapters.kis.realtime import KisRealtimeClient
from danta.config import (
    AppSettings,
    KisCredentials,
    TradingEnvironment,
    load_smtp_config,
)
from danta.db.session import create_engine_and_session
from danta.domain.mandate import EntryMandate
from danta.domain.market import MarketRisk
from danta.domain.market_wide import MarketWideRiskLevel, MarketWideSnapshot
from danta.domain.trading_session import (
    IntentSide,
    OrchestratorState,
    OrderIntent,
    SymbolSession,
    SymbolState,
)
from danta.ports.broker import AccountPosition
from danta.services.autonomous_campaign import (
    AutonomousCampaignController,
)
from danta.services.capital_allocator import CapitalAllocator
from danta.services.command_store import CommandStatus, FileCommandStore, StoredCommand
from danta.services.intraday_candidate_overlay import IntradayCandidateOverlay
from danta.services.market_data_router import MarketDataRouter
from danta.services.market_guard import MarketGuardDecision
from danta.services.market_session import (
    TradingSessionPhase,
    trading_session_phase,
)
from danta.services.market_wide_monitor import (
    MarketStatusPublisher,
    MarketWideCollector,
    MarketWideMonitor,
    is_market_risk_email_transition,
)
from danta.services.market_wide_repository import MarketWideRepository
from danta.services.notifier import NotificationError, SmtpNotifier
from danta.services.order_manager import BrokerReceipt, OrderExecution, OrderManager
from danta.services.policy_registry import TradingPolicyRegistry
from danta.services.priority_intent_scheduler import PriorityIntentScheduler
from danta.services.reconciliation import reconcile_positions
from danta.services.runtime_repository import SqlRuntimeRepository
from danta.services.sql_order_journal import SqlOrderJournal
from danta.services.trade_notification_outbox import (
    TradeNotificationKind,
    TradeNotificationOutbox,
)
from danta.services.trading_orchestrator import TradingOrchestrator
from danta.services.trading_runtime import ManagedPosition, OrderPump, TradingRuntimeCore
from danta.services.unified_market_monitor import UnifiedTradingMonitor

KST = ZoneInfo("Asia/Seoul")


def _ensure_market_resume_latch(
    path: Path,
    *,
    level: MarketWideRiskLevel,
    reasons: tuple[str, ...],
) -> bool:
    """Persist the first risk trip until an operator explicitly removes it."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "triggered_at": datetime.now(UTC).isoformat(),
                "risk_level": level.value,
                "reason_codes": list(reasons),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return True


def _market_monitor_error_requires_latch(observed_at: datetime) -> bool:
    """Only a regular-session sensing failure may persistently stop new entries."""
    return trading_session_phase(observed_at) is TradingSessionPhase.KRX_REGULAR


def _recovery_capital_snapshot(
    available_cash: int,
    broker_positions: list[AccountPosition],
    mandate_symbols: set[str],
) -> int:
    """Rebuild the mandate's capital base without treating held value as new cash."""
    held_cost = sum(
        int(position.average_price * position.quantity)
        for position in broker_positions
        if position.symbol in mandate_symbols
    )
    return available_cash + held_cost


class TradingApplication:
    """Environment-locked KIS executable composition root."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        credentials: KisCredentials,
        mandate: EntryMandate | None,
        policies: TradingPolicyRegistry,
        command_root: Path | None = None,
    ) -> None:
        if credentials.environment is not settings.environment:
            raise PermissionError("runtime and KIS credential environments differ")
        execution_enabled = (
            settings.real_order_execution_enabled
            if settings.environment is TradingEnvironment.PROD
            else settings.paper_order_execution_enabled
        )
        if not execution_enabled:
            raise PermissionError(f"{settings.environment.value} order execution is disabled")
        if not policies.entry.approved_for(settings.environment) or not policies.exit.approved_for(
            settings.environment
        ):
            raise PermissionError(
                "entry and exit policies are not approved for the active environment"
            )
        self.settings = settings
        self.credentials = credentials
        self.mandate = mandate
        self.policies = policies
        resolved_command_root = (
            command_root or Path("private") / settings.environment.value / "commands"
        )
        self.command_store = FileCommandStore(resolved_command_root)
        self.trade_notification_outbox = TradeNotificationOutbox(
            resolved_command_root.parent / "notifications"
        )
        self._notified_price_intents: set[str] = set()
        self._notified_buy_fill_intents: set[str] = set()
        self._notified_stop_intents: set[str] = set()

    @property
    def correlation_id(self) -> str:
        return (
            self.mandate.command_id
            if self.mandate is not None
            else f"{self.settings.environment.value}-account-runtime"
        )

    async def run(self) -> None:
        engine, session_factory = create_engine_and_session(self.settings.database_url)
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        if revision != "0005_fill_ledger":
            await engine.dispose()
            raise RuntimeError("database schema is not current; run alembic upgrade head")
        repository = SqlRuntimeRepository(session_factory)
        market_repository = MarketWideRepository(session_factory)
        notifier: SmtpNotifier | None = None
        if self.settings.smtp_enabled:
            try:
                notifier = SmtpNotifier(load_smtp_config(self.settings))
            except (OSError, ValueError) as exc:
                await repository.audit(
                    "ENTRY_PRICE_EMAIL_CONFIGURATION_ERROR",
                    correlation_id=self.correlation_id,
                    payload={"error": type(exc).__name__},
                )
        price_notifications: asyncio.Queue[tuple[str, str, int]] = asyncio.Queue()
        buy_fill_notifications: asyncio.Queue[tuple[str, str, int, int]] = asyncio.Queue()
        stop_notifications: asyncio.Queue[tuple[str, str, int, Decimal, str]] = asyncio.Queue()
        if self.mandate is not None:
            self.command_store.submit(self.mandate)
        active_command = self.command_store.accept_next()
        self.mandate = None if active_command is None else active_command.mandate
        selection_names = {
            selection.symbol: selection.name
            for selection in ([] if self.mandate is None else self.mandate.selections)
        }
        recovery_command_id = None if active_command is None else active_command.mandate.command_id
        recovery_selection_names = dict(selection_names)
        scheduler = PriorityIntentScheduler()
        orchestrator = TradingOrchestrator(
            capital_allocator=CapitalAllocator(),
            scheduler=scheduler,
            max_approved_symbols=self.settings.maximum_managed_symbols,
        )
        core = TradingRuntimeCore(
            orchestrator=orchestrator,
            entry_policy=self.policies.entry.to_domain(self.settings.environment),
            exit_policy=self.policies.exit.to_domain(self.settings.environment),
        )
        if self.settings.market_entry_resume_required_path.exists():
            core.require_market_entry_resume_confirmation()
        async with KisClient(
            self.credentials,
            token_cache_path=self.settings.kis_token_cache_path,
            order_submission_enabled=True,
        ) as broker:
            broker_positions = await broker.positions()
            stored_positions = await repository.load_open_positions()
            broker_by_symbol = {position.symbol: position for position in broker_positions}
            for stored in stored_positions:
                broker_position = broker_by_symbol.get(stored.symbol)
                sellable = broker_position.sellable_quantity if broker_position is not None else 0
                orchestrator.sessions[stored.symbol] = SymbolSession(
                    symbol=stored.symbol,
                    generation=stored.generation,
                    state=SymbolState.POSITION_OPEN,
                    quantity=stored.quantity,
                    sellable_quantity=sellable,
                )
            reconciliation = reconcile_positions(broker_positions, orchestrator.sessions)
            await orchestrator.reconcile_complete(
                safe_for_new_entries=reconciliation.safe_for_new_entries
            )
            await repository.audit(
                "STARTUP_RECONCILIATION",
                correlation_id=self.correlation_id,
                payload={
                    "safe_for_new_entries": reconciliation.safe_for_new_entries,
                    "issues": [
                        {
                            "symbol": issue.symbol,
                            "code": issue.code,
                            "broker_quantity": issue.broker_quantity,
                            "internal_quantity": issue.internal_quantity,
                        }
                        for issue in reconciliation.issues
                    ],
                },
            )
            for stored in stored_positions:
                broker_position = broker_by_symbol.get(stored.symbol)
                if broker_position is None:
                    orchestrator.sessions[stored.symbol].state = SymbolState.QUARANTINED
                    orchestrator.sessions[stored.symbol].quantity = 0
                    orchestrator.sessions[stored.symbol].sellable_quantity = 0
                    await repository.close_position(
                        symbol=stored.symbol,
                        generation=stored.generation,
                    )
                    continue
                session = orchestrator.sessions[stored.symbol]
                session.quantity = broker_position.quantity
                session.sellable_quantity = broker_position.sellable_quantity
                session.state = (
                    SymbolState.POSITION_OPEN
                    if stored.quantity == broker_position.quantity
                    else SymbolState.QUARANTINED
                )
                recovered_position = ManagedPosition(
                    symbol=stored.symbol,
                    generation=stored.generation,
                    quantity=broker_position.quantity,
                    sellable_quantity=broker_position.sellable_quantity,
                    average_entry_price=broker_position.average_price,
                    opened_at=stored.opened_at,
                    peak_return_pct=stored.peak_return_pct,
                )
                core.restore_position(recovered_position)
                await repository.save_position(recovered_position)
            if reconciliation.discovered_positions:
                latest_discovered = await repository.latest_generations(
                    [position.symbol for position in reconciliation.discovered_positions]
                )
                for position in reconciliation.discovered_positions:
                    generation = latest_discovered.get(position.symbol, -1) + 1
                    recovered = ManagedPosition(
                        symbol=position.symbol,
                        generation=generation,
                        quantity=position.quantity,
                        sellable_quantity=position.sellable_quantity,
                        average_entry_price=position.average_price,
                        opened_at=datetime.now(UTC),
                    )
                    orchestrator.sessions[position.symbol] = SymbolSession(
                        symbol=position.symbol,
                        generation=generation,
                        state=SymbolState.QUARANTINED,
                        quantity=position.quantity,
                        sellable_quantity=position.sellable_quantity,
                    )
                    core.restore_position(recovered)
                    await repository.save_position(recovered)
            if active_command is not None and await self._startup_lifecycle_complete(
                active_command=active_command,
                broker=broker,
                broker_position_symbols=set(broker_by_symbol),
                repository=repository,
            ):
                completed_command_id = active_command.mandate.command_id
                selected_symbols = [
                    selection.symbol for selection in active_command.mandate.selections
                ]
                self.command_store.archive_active(
                    completed_command_id,
                    status=CommandStatus.COMPLETED,
                    reason="STARTUP_CONFIRMED_LIFECYCLE_COMPLETE_AND_FLAT",
                )
                await repository.audit(
                    "MANDATE_COMPLETED_ON_STARTUP",
                    correlation_id=completed_command_id,
                    payload={
                        "symbols": selected_symbols,
                        "reason": "KIS_FLAT_NO_OPEN_ORDERS_AND_DB_LIFECYCLE_CLOSED",
                    },
                )
                active_command = None
                self.mandate = None
                selection_names = {}
            if reconciliation.safe_for_new_entries and self.mandate is not None:
                mandate_symbols = [selection.symbol for selection in self.mandate.selections]
                latest_generations = await repository.latest_generations(mandate_symbols)
                for symbol, generation in latest_generations.items():
                    if symbol not in orchestrator.sessions:
                        orchestrator.sessions[symbol] = SymbolSession(
                            symbol=symbol,
                            generation=generation,
                            state=SymbolState.CLOSED,
                        )
                reference = self.mandate.selections[0]
                cash = await broker.orderable_cash(
                    reference.symbol,
                    reference_price=reference.entry_target_price_krw,
                )
                recovery_capital = _recovery_capital_snapshot(
                    cash.amount,
                    broker_positions,
                    set(mandate_symbols),
                )
                await core.activate_mandate(
                    self.mandate,
                    orderable_cash=recovery_capital,
                )
                completed_symbols: set[str] = set()
                if active_command is not None:
                    accepted_at = active_command.accepted_at
                    if accepted_at.tzinfo is None:
                        accepted_at = accepted_at.replace(tzinfo=UTC)
                    completed_symbols = await repository.closed_symbols_since(
                        mandate_symbols,
                        opened_since=accepted_at,
                    )
                    completed_symbols.difference_update(broker_by_symbol)
                    if completed_symbols:
                        core.restore_completed_symbols(completed_symbols)
                        await repository.audit(
                            "MANDATE_COMPLETED_LEGS_RESTORED",
                            correlation_id=self.mandate.command_id,
                            payload={
                                "symbols": sorted(completed_symbols),
                                "reason": "DB_LIFECYCLE_CLOSED_AND_KIS_FLAT",
                            },
                        )
                await repository.audit(
                    "MANDATE_ACTIVATED",
                    correlation_id=self.correlation_id,
                    payload={
                        "symbols": list(core.signals),
                        "orderable_cash": cash.amount,
                        "recovery_capital_snapshot": recovery_capital,
                        "entry_policy": self.policies.entry.version,
                        "exit_policy": self.policies.exit.version,
                    },
                )
            elif not reconciliation.safe_for_new_entries:
                await orchestrator.reconcile_complete(safe_for_new_entries=False)
            journal = SqlOrderJournal(session_factory)
            await self._recover_orders(
                core,
                broker,
                journal,
                repository,
                buy_fill_notifications,
                stop_notifications,
                recovery_command_id=recovery_command_id,
                recovery_selection_names=recovery_selection_names,
            )
            if notifier is not None:
                await self._replay_trade_notification_outbox(
                    buy_fill_notifications,
                    stop_notifications,
                    repository,
                )
            realtime = KisRealtimeClient(self.credentials)
            manager = OrderManager(
                _KisOrderBrokerAdapter(broker),
                journal,
            )
            pump = OrderPump(
                core=core,
                manager=manager,
                on_error=lambda intent, error: repository.audit(
                    "ORDER_SUBMISSION_ERROR",
                    correlation_id=self.correlation_id,
                    payload={
                        "symbol": intent.symbol,
                        "side": intent.side.value,
                        "intent_key": intent.idempotency_key,
                        "error": type(error).__name__,
                    },
                ),
                on_intent_ready=(
                    None
                    if notifier is None
                    else lambda intent: self._queue_entry_price_notification(
                        intent,
                        price_notifications,
                        selection_names,
                        repository,
                    )
                ),
            )
            router = MarketDataRouter(
                core,
                on_error=lambda symbol, error: repository.audit(
                    "SYMBOL_MARKET_WORKER_ERROR",
                    correlation_id=self.correlation_id,
                    payload={"symbol": symbol, "error": type(error).__name__},
                ),
            )
            router.start(core.signals)
            market_monitor = MarketWideMonitor(
                collector=MarketWideCollector(broker),
                repository=market_repository,
                on_transition=lambda snapshot, decision, previous: self._market_risk_transition(
                    snapshot,
                    decision,
                    previous,
                    # Market-transition email is intentionally disabled by
                    # policy. The unified runtime remains the single collector
                    # and still applies and audits the market risk gate.
                    None,
                    repository,
                ),
            )
            market_publisher = (
                MarketStatusPublisher(
                    repository_path=self.settings.market_dashboard_publish_repo,
                    git_push_enabled=self.settings.market_pages_git_push_enabled,
                )
                if self.settings.market_pages_publish_enabled
                else None
            )
            try:
                async with asyncio.TaskGroup() as group:
                    group.create_task(
                        UnifiedTradingMonitor(
                            realtime=realtime,
                            router=router,
                            core=core,
                            premarket_policy=self.policies.premarket.to_domain(
                                self.settings.environment
                            ),
                            repository=repository,
                            correlation_id=f"{self.settings.environment.value}-account-runtime",
                            opening_reconcile=lambda: self._opening_reconcile(
                                core,
                                broker,
                                repository,
                            ),
                        ).run(),
                        name="danta-unified-market-monitor",
                    )
                    group.create_task(
                        self._watch_command_inbox(
                            core,
                            broker,
                            router,
                            repository,
                            selection_names,
                        ),
                        name="danta-command-inbox",
                    )
                    group.create_task(
                        AutonomousCampaignController(
                            settings=self.settings,
                            credentials=self.credentials,
                            command_store=self.command_store,
                            core=core,
                            repository=repository,
                            notifier=notifier,
                            broker=broker,
                        ).run(),
                        name="danta-autonomous-campaign",
                    )
                    if self.settings.intraday_overlay_enabled:
                        group.create_task(
                            IntradayCandidateOverlay(
                                settings=self.settings,
                                broker=broker,
                                repository=repository,
                            ).run(),
                            name="danta-intraday-candidate-overlay",
                        )
                    group.create_task(pump.run(), name="danta-order-pump")
                    group.create_task(
                        self._poll_orders(
                            core,
                            broker,
                            manager,
                            journal,
                            repository,
                            buy_fill_notifications,
                            stop_notifications,
                            selection_names,
                        ),
                        name="danta-order-reconciliation",
                    )
                    group.create_task(
                        self._hard_stop_watchdog(core, broker, repository),
                        name="danta-rest-hard-stop-watchdog",
                    )
                    if self.settings.market_wide_monitor_enabled:
                        group.create_task(
                            self._run_market_wide_monitor(
                                market_monitor,
                                core,
                                market_publisher,
                                repository,
                            ),
                            name="danta-market-wide-monitor",
                        )
                    if notifier is not None:
                        group.create_task(
                            self._send_entry_price_notifications(
                                price_notifications,
                                notifier,
                                repository,
                            ),
                            name="danta-entry-price-email",
                        )
                        group.create_task(
                            self._send_buy_fill_notifications(
                                buy_fill_notifications,
                                notifier,
                                repository,
                            ),
                            name="danta-buy-fill-email",
                        )
                        group.create_task(
                            self._send_stop_loss_notifications(
                                stop_notifications,
                                notifier,
                                repository,
                            ),
                            name="danta-stop-loss-email",
                        )
            finally:
                await router.stop()
                await realtime.close()
                await engine.dispose()

    async def _startup_lifecycle_complete(
        self,
        *,
        active_command: StoredCommand,
        broker: KisClient,
        broker_position_symbols: set[str],
        repository: SqlRuntimeRepository,
    ) -> bool:
        """Reject stale ACTIVE files whose entire broker lifecycle has ended.

        KIS remains authoritative.  A command is terminal only when every selected
        symbol is flat at KIS, every symbol has a DB position opened after command
        acceptance and now closed, and no selected symbol has a remaining order.
        Any KIS lookup failure propagates and prevents accidental re-entry.
        """
        symbols = [selection.symbol for selection in active_command.mandate.selections]
        selected = set(symbols)
        if selected & broker_position_symbols:
            return False

        accepted_at = active_command.accepted_at
        if accepted_at.tzinfo is None:
            accepted_at = accepted_at.replace(tzinfo=UTC)
        closed = await repository.closed_symbols_since(
            symbols,
            opened_since=accepted_at,
        )
        if not selected.issubset(closed):
            return False

        trading_dates = {
            accepted_at.astimezone(KST).strftime("%Y%m%d"),
            datetime.now(KST).strftime("%Y%m%d"),
        }
        for trading_date in sorted(trading_dates):
            statuses = await broker.daily_order_statuses(trading_date=trading_date)
            if any(
                status.symbol in selected and status.remaining_quantity > 0 for status in statuses
            ):
                return False
        return True

    async def _recover_orders(
        self,
        core: TradingRuntimeCore,
        broker: KisClient,
        journal: SqlOrderJournal,
        repository: SqlRuntimeRepository,
        buy_fill_notifications: asyncio.Queue[tuple[str, str, int, int]],
        stop_notifications: asyncio.Queue[tuple[str, str, int, Decimal, str]],
        *,
        recovery_command_id: str | None,
        recovery_selection_names: dict[str, str],
    ) -> None:
        """Reconnect durable intents to KIS order numbers before workers start."""
        recoverable = await journal.load_recoverable()
        if not recoverable:
            await repository.audit(
                "STARTUP_ORDER_RECOVERY",
                correlation_id=self.correlation_id,
                payload={"recoverable": 0, "linked": 0, "quarantined": 0},
            )
            return
        dates = {item.intent.created_at.astimezone(KST).strftime("%Y%m%d") for item in recoverable}
        dates.add(datetime.now(KST).strftime("%Y%m%d"))
        by_date_and_number: dict[tuple[str, str], KisOrderStatus] = {}
        for trading_date in sorted(dates):
            for status in await broker.daily_order_statuses(trading_date=trading_date):
                by_date_and_number[(trading_date, status.broker_order_no)] = status
        linked = 0
        quarantined = 0
        recovered_notifications = 0
        sent_notification_keys = await repository.sent_trade_notification_intent_keys()
        for item in recoverable:
            broker_no = item.broker_order_no
            trading_date = item.intent.created_at.astimezone(KST).strftime("%Y%m%d")
            recovered_status = (
                None if broker_no is None else by_date_and_number.get((trading_date, broker_no))
            )
            if broker_no is None or recovered_status is None:
                quarantined += 1
                core.orchestrator.state = OrchestratorState.ENTRY_BLOCKED
                session = core.orchestrator.sessions.get(item.intent.symbol)
                if session is not None:
                    session.state = SymbolState.QUARANTINED
                await repository.audit(
                    "RECOVERED_ORDER_QUARANTINED",
                    correlation_id=self.correlation_id,
                    payload={
                        "intent_key": item.intent.idempotency_key,
                        "symbol": item.intent.symbol,
                        "reason": (
                            "BROKER_ORDER_NUMBER_MISSING"
                            if broker_no is None
                            else "BROKER_STATUS_NOT_FOUND"
                        ),
                    },
                )
                continue
            await journal.apply_broker_status(
                idempotency_key=item.intent.idempotency_key,
                broker_order_no=broker_no,
                symbol=item.intent.symbol,
                ordered_quantity=recovered_status.ordered_quantity,
                cumulative_filled_quantity=recovered_status.filled_quantity,
                average_fill_price=recovered_status.average_fill_price,
                remaining_quantity=recovered_status.remaining_quantity,
                filled_at=datetime.now(UTC),
            )
            if recovered_status.remaining_quantity <= 0:
                if (
                    recovery_command_id is not None
                    and item.intent.approval_id == recovery_command_id
                    and item.intent.idempotency_key not in sent_notification_keys
                    and recovered_status.filled_quantity > 0
                ):
                    recovered_notifications += await self._recover_completed_fill_email(
                        item.intent,
                        recovered_status,
                        repository,
                        buy_fill_notifications,
                        stop_notifications,
                        recovery_selection_names,
                    )
                continue
            if item.intent.symbol not in core.orchestrator.sessions:
                quarantined += 1
                core.orchestrator.state = OrchestratorState.ENTRY_BLOCKED
                await repository.audit(
                    "RECOVERED_ORDER_QUARANTINED",
                    correlation_id=self.correlation_id,
                    payload={
                        "intent_key": item.intent.idempotency_key,
                        "symbol": item.intent.symbol,
                        "reason": "SYMBOL_SESSION_MISSING",
                    },
                )
                continue
            core.restore_submission(
                item.intent,
                OrderExecution(
                    idempotency_key=item.intent.idempotency_key,
                    broker_order_no=broker_no,
                    status=item.broker_status or "SUBMITTED",
                ),
                cumulative_filled=recovered_status.filled_quantity,
                average_fill_price=recovered_status.average_fill_price,
            )
            linked += 1
        await repository.audit(
            "STARTUP_ORDER_RECOVERY",
            correlation_id=self.correlation_id,
            payload={
                "recoverable": len(recoverable),
                "linked": linked,
                "quarantined": quarantined,
                "recovered_notifications": recovered_notifications,
            },
        )

    async def _recover_completed_fill_email(
        self,
        intent: OrderIntent,
        status: KisOrderStatus,
        repository: SqlRuntimeRepository,
        buy_queue: asyncio.Queue[tuple[str, str, int, int]],
        exit_queue: asyncio.Queue[tuple[str, str, int, Decimal, str]],
        selection_names: dict[str, str],
    ) -> int:
        if intent.side is IntentSide.BUY:
            await self._queue_buy_fill_notification(
                intent,
                status,
                buy_queue,
                selection_names,
                repository,
            )
            return 1 if intent.idempotency_key in self._notified_buy_fill_intents else 0

        average_entry_price = await repository.position_average_entry_price(
            symbol=intent.symbol,
            generation=intent.generation,
        )
        if average_entry_price is None:
            await repository.audit(
                "EXIT_FILL_EMAIL_RECOVERY_BLOCKED",
                correlation_id=intent.approval_id,
                payload={
                    "intent_key": intent.idempotency_key,
                    "reason": "AVERAGE_ENTRY_PRICE_NOT_FOUND",
                },
            )
            return 0
        recovered_position = ManagedPosition(
            symbol=intent.symbol,
            generation=intent.generation,
            quantity=max(1, status.filled_quantity),
            sellable_quantity=max(1, status.filled_quantity),
            average_entry_price=average_entry_price,
            opened_at=intent.created_at,
        )
        await self._queue_stop_loss_notification(
            intent,
            status,
            recovered_position,
            exit_queue,
            selection_names,
            repository,
        )
        return 1 if intent.idempotency_key in self._notified_stop_intents else 0

    async def _watch_command_inbox(
        self,
        core: TradingRuntimeCore,
        broker: KisClient,
        router: MarketDataRouter,
        repository: SqlRuntimeRepository,
        selection_names: dict[str, str],
    ) -> None:
        """Accept a new mandate while the account runtime stays alive."""
        while True:
            try:
                if self.mandate is not None:
                    terminal_states = {
                        SymbolState.CLOSED,
                        SymbolState.INVALIDATED,
                    }
                    terminal = all(
                        core.orchestrator.sessions[selection.symbol].state in terminal_states
                        for selection in self.mandate.selections
                    )
                    pending = any(
                        core.orchestrator.sessions[selection.symbol].state
                        in {
                            SymbolState.BUY_PENDING,
                            SymbolState.PARTIALLY_FILLED,
                            SymbolState.SELL_PENDING,
                            SymbolState.QUARANTINED,
                        }
                        for selection in self.mandate.selections
                    )
                    if terminal and not pending:
                        completed_id = self.mandate.command_id
                        self.command_store.archive_active(
                            completed_id,
                            status=CommandStatus.COMPLETED,
                            reason="ALL_SYMBOLS_TERMINAL_AND_FLAT",
                        )
                        terminal_symbols = [
                            selection.symbol for selection in self.mandate.selections
                        ]
                        core.clear_terminal_mandate()
                        self.mandate = None
                        await router.stop_symbols(terminal_symbols)
                        await repository.audit(
                            "MANDATE_COMPLETED",
                            correlation_id=completed_id,
                            payload={"reason": "ALL_SYMBOLS_TERMINAL_AND_FLAT"},
                        )
                active = self.command_store.load_active()
                if self.mandate is None and active is None:
                    active = self.command_store.accept_next()
                if self.mandate is None and active is not None:
                    managed = {
                        symbol
                        for symbol, position in core.positions.items()
                        if position.quantity > 0
                    }
                    requested = {selection.symbol for selection in active.mandate.selections}
                    if len(managed | requested) > self.settings.maximum_managed_symbols:
                        self.command_store.archive_active(
                            active.mandate.command_id,
                            status=CommandStatus.REJECTED,
                            reason="ACCOUNT_SYMBOL_LIMIT_EXCEEDED",
                        )
                        await repository.audit(
                            "MANDATE_REJECTED",
                            correlation_id=active.mandate.command_id,
                            payload={"reason": "ACCOUNT_SYMBOL_LIMIT_EXCEEDED"},
                        )
                    elif core.orchestrator.state is OrchestratorState.RUNNING:
                        reference = active.mandate.selections[0]
                        cash = await broker.orderable_cash(
                            reference.symbol,
                            reference_price=reference.entry_target_price_krw,
                        )
                        await core.activate_mandate(
                            active.mandate,
                            orderable_cash=cash.amount,
                        )
                        self.mandate = active.mandate
                        selection_names.update(
                            {
                                selection.symbol: selection.name
                                for selection in active.mandate.selections
                            }
                        )
                        router.start(core.signals)
                        await repository.audit(
                            "MANDATE_ACTIVATED",
                            correlation_id=active.mandate.command_id,
                            payload={
                                "symbols": list(core.signals),
                                "orderable_cash": cash.amount,
                                "source": "COMMAND_INBOX",
                            },
                        )
                self.command_store.write_runtime_state(
                    {
                        "pid": __import__("os").getpid(),
                        "orchestrator_state": core.orchestrator.state.value,
                        "active_command_id": (
                            None if self.mandate is None else self.mandate.command_id
                        ),
                        "managed_positions": sorted(core.positions),
                        "pending_orders": sorted(core.submitted),
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await repository.audit(
                    "COMMAND_INBOX_ERROR",
                    correlation_id=self.correlation_id,
                    payload={
                        "error": type(exc).__name__,
                        "detail": str(exc)[:240],
                    },
                )
            await self.command_store.wait_for_change()

    async def _run_market_wide_monitor(
        self,
        monitor: MarketWideMonitor,
        core: TradingRuntimeCore,
        publisher: MarketStatusPublisher | None,
        repository: SqlRuntimeRepository,
    ) -> None:
        poll_interval = float(self.settings.market_wide_poll_interval_seconds)
        publish_interval = float(self.settings.market_pages_publish_interval_seconds)
        next_publish_at = 0.0
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                snapshot, decision = await monitor.poll_once()
                core.set_market_guard(
                    decision.risk,
                    stress_score=decision.stress_score,
                )
                latch_created = False
                if decision.risk is MarketRisk.RISK_OFF:
                    latch_created = _ensure_market_resume_latch(
                        self.settings.market_entry_resume_required_path,
                        level=decision.level,
                        reasons=decision.reason_codes,
                    )
                if self.settings.market_entry_resume_required_path.exists():
                    core.require_market_entry_resume_confirmation()
                elif core.market_entry_resume_required:
                    core.acknowledge_market_entry_resume()
                if latch_created:
                    await repository.audit(
                        "MARKET_ENTRY_RESUME_CONFIRMATION_REQUIRED",
                        correlation_id=self.correlation_id,
                        payload={
                            "risk_level": decision.level.value,
                            "reason_codes": list(decision.reason_codes),
                        },
                    )
                await repository.audit(
                    "MARKET_WIDE_SNAPSHOT",
                    correlation_id=self.correlation_id,
                    payload={
                        "risk_level": decision.level.value,
                        "entry_guard": decision.risk.value,
                        "stress_score": str(decision.stress_score),
                        "kospi_return_pct": str(snapshot.kospi_return_pct),
                        "declining_issue_ratio": str(snapshot.declining_issue_ratio),
                        "foreign_net_million": snapshot.investor.foreign,
                        "pension_net_million": (snapshot.investor.pension_fund_etc),
                        "program_net_million": snapshot.program.total,
                    },
                )
                if publisher is not None and loop.time() >= next_publish_at:
                    try:
                        target = await publisher.publish(snapshot, decision)
                        await repository.audit(
                            "MARKET_STATUS_PUBLISHED",
                            correlation_id=self.correlation_id,
                            payload={"path": str(target)},
                        )
                    except Exception as exc:
                        await repository.audit(
                            "MARKET_STATUS_PUBLISH_ERROR",
                            correlation_id=self.correlation_id,
                            payload={"error": type(exc).__name__},
                        )
                    finally:
                        next_publish_at = loop.time() + publish_interval
            except Exception as exc:
                # A broken market-wide feed blocks only new entries. Position
                # monitoring and protective exits continue in their own tasks.
                observed_at = datetime.now(UTC)
                latch_required = _market_monitor_error_requires_latch(observed_at)
                if latch_required:
                    core.set_market_guard(MarketRisk.RISK_OFF, stress_score=Decimal("1"))
                    core.require_market_entry_resume_confirmation()
                    _ensure_market_resume_latch(
                        self.settings.market_entry_resume_required_path,
                        level=MarketWideRiskLevel.RISK_OFF,
                        reasons=("MARKET_WIDE_MONITOR_ERROR",),
                    )
                await repository.audit(
                    "MARKET_WIDE_MONITOR_ERROR",
                    correlation_id=self.correlation_id,
                    payload={
                        "error": type(exc).__name__,
                        "detail": str(exc)[:240],
                        "session_phase": trading_session_phase(observed_at).value,
                        "persistent_entry_latch": latch_required,
                    },
                )
            elapsed = loop.time() - started
            await asyncio.sleep(max(1.0, poll_interval - elapsed))

    async def _market_risk_transition(
        self,
        snapshot: MarketWideSnapshot,
        decision: MarketGuardDecision,
        previous: MarketWideRiskLevel | None,
        notifier: SmtpNotifier | None,
        repository: SqlRuntimeRepository,
    ) -> None:
        await repository.audit(
            "MARKET_RISK_STATE_CHANGED",
            correlation_id=self.correlation_id,
            payload={
                "previous": None if previous is None else previous.value,
                "current": decision.level.value,
                "reason_codes": list(decision.reason_codes),
            },
        )
        if not is_market_risk_email_transition(previous, decision.level):
            await repository.audit(
                "MARKET_RISK_EMAIL_SUPPRESSED",
                correlation_id=self.correlation_id,
                payload={
                    "previous": None if previous is None else previous.value,
                    "current": decision.level.value,
                    "reason": "NOT_A_STATE_TRANSITION",
                },
            )
            return
        if notifier is None:
            return
        try:
            receipt = await asyncio.to_thread(
                notifier.send_market_risk_transition,
                previous=previous,
                current=decision.level,
                kospi_return_pct=snapshot.kospi_return_pct,
                foreign_net_million=snapshot.investor.foreign,
                institution_net_million=snapshot.investor.institution,
                pension_net_million=snapshot.investor.pension_fund_etc,
                program_net_million=snapshot.program.total,
                reasons=decision.reason_codes,
                dashboard_url=self.settings.market_dashboard_public_url,
            )
            await repository.audit(
                "MARKET_RISK_EMAIL_SENT",
                correlation_id=self.correlation_id,
                payload={"recipient_count": receipt.recipient_count},
            )
        except (NotificationError, OSError) as exc:
            await repository.audit(
                "MARKET_RISK_EMAIL_ERROR",
                correlation_id=self.correlation_id,
                payload={"error": type(exc).__name__},
            )

    async def _queue_entry_price_notification(
        self,
        intent: OrderIntent,
        queue: asyncio.Queue[tuple[str, str, int]],
        selection_names: dict[str, str],
        repository: SqlRuntimeRepository,
    ) -> None:
        if (
            intent.side is not IntentSide.BUY
            or intent.limit_price is None
            or intent.idempotency_key in self._notified_price_intents
        ):
            return
        self._notified_price_intents.add(intent.idempotency_key)
        await queue.put(
            (
                intent.idempotency_key,
                selection_names.get(intent.symbol, intent.symbol),
                intent.limit_price,
            )
        )
        await repository.audit(
            "ENTRY_LIMIT_PRICE_DETERMINED",
            correlation_id=self.correlation_id,
            payload={
                "symbol": intent.symbol,
                "limit_price": intent.limit_price,
                "intent_key": intent.idempotency_key,
            },
        )

    async def _send_entry_price_notifications(
        self,
        queue: asyncio.Queue[tuple[str, str, int]],
        notifier: SmtpNotifier,
        repository: SqlRuntimeRepository,
    ) -> None:
        retry_delays = (2.0, 10.0)
        while True:
            intent_key, name, price = await queue.get()
            try:
                for attempt in range(3):
                    try:
                        receipt = await asyncio.to_thread(
                            notifier.send_entry_prices_determined,
                            [(name, price)],
                        )
                        await repository.audit(
                            "ENTRY_LIMIT_PRICE_EMAIL_SENT",
                            correlation_id=self.correlation_id,
                            payload={
                                "intent_key": intent_key,
                                "recipient_count": receipt.recipient_count,
                            },
                        )
                        break
                    except (NotificationError, OSError) as exc:
                        await repository.audit(
                            "ENTRY_LIMIT_PRICE_EMAIL_ERROR",
                            correlation_id=self.correlation_id,
                            payload={
                                "intent_key": intent_key,
                                "attempt": attempt + 1,
                                "error": type(exc).__name__,
                            },
                        )
                        if attempt < len(retry_delays):
                            await asyncio.sleep(retry_delays[attempt])
            finally:
                queue.task_done()

    async def _queue_stop_loss_notification(
        self,
        intent: OrderIntent,
        status: KisOrderStatus,
        position_before: ManagedPosition | None,
        queue: asyncio.Queue[tuple[str, str, int, Decimal, str]],
        selection_names: dict[str, str],
        repository: SqlRuntimeRepository,
    ) -> None:
        if (
            intent.side is not IntentSide.SELL
            or status.remaining_quantity != 0
            or status.filled_quantity <= 0
            or position_before is None
            or intent.idempotency_key in self._notified_stop_intents
        ):
            return
        return_pct = (
            (status.average_fill_price - position_before.average_entry_price)
            / position_before.average_entry_price
            * Decimal("100")
        )
        name = selection_names.get(intent.symbol, intent.symbol)
        outbox = getattr(self, "trade_notification_outbox", None)
        if outbox is not None and not outbox.enqueue_exit(
            intent_key=intent.idempotency_key,
            correlation_id=self.correlation_id,
            name=name,
            price=int(status.average_fill_price),
            return_pct=return_pct,
            cause=intent.cause,
        ):
            self._notified_stop_intents.add(intent.idempotency_key)
            return
        self._notified_stop_intents.add(intent.idempotency_key)
        await queue.put(
            (
                intent.idempotency_key,
                name,
                int(status.average_fill_price),
                return_pct,
                intent.cause,
            )
        )
        await repository.audit(
            "EXIT_FILL_EMAIL_QUEUED",
            correlation_id=self.correlation_id,
            payload={
                "symbol": intent.symbol,
                "average_fill_price": int(status.average_fill_price),
                "return_pct": str(return_pct),
                "cause": intent.cause,
                "intent_key": intent.idempotency_key,
            },
        )

    async def _queue_buy_fill_notification(
        self,
        intent: OrderIntent,
        status: KisOrderStatus,
        queue: asyncio.Queue[tuple[str, str, int, int]],
        selection_names: dict[str, str],
        repository: SqlRuntimeRepository,
    ) -> None:
        if (
            intent.side is not IntentSide.BUY
            or status.remaining_quantity != 0
            or status.filled_quantity <= 0
            or intent.idempotency_key in self._notified_buy_fill_intents
        ):
            return
        name = selection_names.get(intent.symbol, intent.symbol)
        outbox = getattr(self, "trade_notification_outbox", None)
        if outbox is not None and not outbox.enqueue_buy(
            intent_key=intent.idempotency_key,
            correlation_id=self.correlation_id,
            name=name,
            price=int(status.average_fill_price),
            quantity=status.filled_quantity,
        ):
            self._notified_buy_fill_intents.add(intent.idempotency_key)
            return
        self._notified_buy_fill_intents.add(intent.idempotency_key)
        item = (
            intent.idempotency_key,
            name,
            int(status.average_fill_price),
            status.filled_quantity,
        )
        await queue.put(item)
        await repository.audit(
            "ENTRY_FILL_EMAIL_QUEUED",
            correlation_id=self.correlation_id,
            payload={
                "symbol": intent.symbol,
                "average_fill_price": int(status.average_fill_price),
                "filled_quantity": status.filled_quantity,
                "intent_key": intent.idempotency_key,
            },
        )

    async def _send_buy_fill_notifications(
        self,
        queue: asyncio.Queue[tuple[str, str, int, int]],
        notifier: SmtpNotifier,
        repository: SqlRuntimeRepository,
    ) -> None:
        retry_delays = (2.0, 10.0)
        while True:
            intent_key, name, price, quantity = await queue.get()
            try:
                for attempt in range(3):
                    try:
                        receipt = await asyncio.to_thread(
                            notifier.send_buy_completed,
                            [(name, price, quantity)],
                        )
                        outbox = getattr(self, "trade_notification_outbox", None)
                        if outbox is not None:
                            outbox.mark_sent(
                                kind=TradeNotificationKind.BUY,
                                intent_key=intent_key,
                            )
                        await repository.audit(
                            "ENTRY_FILL_EMAIL_SENT",
                            correlation_id=self.correlation_id,
                            payload={
                                "intent_key": intent_key,
                                "recipient_count": receipt.recipient_count,
                            },
                        )
                        break
                    except (NotificationError, OSError) as exc:
                        await repository.audit(
                            "ENTRY_FILL_EMAIL_ERROR",
                            correlation_id=self.correlation_id,
                            payload={
                                "intent_key": intent_key,
                                "attempt": attempt + 1,
                                "error": type(exc).__name__,
                            },
                        )
                        if attempt < len(retry_delays):
                            await asyncio.sleep(retry_delays[attempt])
            finally:
                queue.task_done()

    async def _send_stop_loss_notifications(
        self,
        queue: asyncio.Queue[tuple[str, str, int, Decimal, str]],
        notifier: SmtpNotifier,
        repository: SqlRuntimeRepository,
    ) -> None:
        retry_delays = (2.0, 10.0)
        while True:
            intent_key, name, price, return_pct, cause = await queue.get()
            try:
                for attempt in range(3):
                    try:
                        if cause in {
                            "EARLY_DEFENSE",
                            "HARD_DEFENSE_MINUS_5",
                            "HARD_STOP_MINUS_7",
                        }:
                            receipt = await asyncio.to_thread(
                                notifier.send_stop_loss_completed,
                                [(name, price, return_pct)],
                            )
                        else:
                            receipt = await asyncio.to_thread(
                                notifier.send_exit_completed,
                                [(name, price, return_pct, cause)],
                            )
                        outbox = getattr(self, "trade_notification_outbox", None)
                        if outbox is not None:
                            outbox.mark_sent(
                                kind=TradeNotificationKind.EXIT,
                                intent_key=intent_key,
                            )
                        await repository.audit(
                            "EXIT_FILL_EMAIL_SENT",
                            correlation_id=self.correlation_id,
                            payload={
                                "intent_key": intent_key,
                                "cause": cause,
                                "recipient_count": receipt.recipient_count,
                            },
                        )
                        break
                    except (NotificationError, OSError) as exc:
                        await repository.audit(
                            "EXIT_FILL_EMAIL_ERROR",
                            correlation_id=self.correlation_id,
                            payload={
                                "intent_key": intent_key,
                                "cause": cause,
                                "attempt": attempt + 1,
                                "error": type(exc).__name__,
                            },
                        )
                        if attempt < len(retry_delays):
                            await asyncio.sleep(retry_delays[attempt])
            finally:
                queue.task_done()

    async def _replay_trade_notification_outbox(
        self,
        buy_queue: asyncio.Queue[tuple[str, str, int, int]],
        exit_queue: asyncio.Queue[tuple[str, str, int, Decimal, str]],
        repository: SqlRuntimeRepository,
    ) -> None:
        for item in self.trade_notification_outbox.load_pending():
            if item.kind is TradeNotificationKind.BUY:
                if item.quantity is None:
                    raise RuntimeError("durable buy notification has no quantity")
                self._notified_buy_fill_intents.add(item.intent_key)
                await buy_queue.put((item.intent_key, item.name, item.price, item.quantity))
            else:
                if item.return_pct is None or item.cause is None:
                    raise RuntimeError("durable exit notification is incomplete")
                self._notified_stop_intents.add(item.intent_key)
                await exit_queue.put(
                    (
                        item.intent_key,
                        item.name,
                        item.price,
                        item.return_pct,
                        item.cause,
                    )
                )
            await repository.audit(
                "TRADE_FILL_EMAIL_REPLAYED",
                correlation_id=item.correlation_id,
                payload={
                    "intent_key": item.intent_key,
                    "kind": item.kind.value,
                },
            )

    async def _opening_reconcile(
        self,
        core: TradingRuntimeCore,
        broker: KisClient,
        repository: SqlRuntimeRepository,
    ) -> bool:
        """Reconcile account state immediately before releasing opening exits."""
        try:
            broker_positions = await broker.positions()
        except KisApiError as exc:
            await core.orchestrator.reconcile_complete(safe_for_new_entries=False)
            await repository.audit(
                "OPENING_ACCOUNT_RECONCILIATION_ERROR",
                correlation_id=self.correlation_id,
                payload={
                    "error": type(exc).__name__,
                    "status_code": exc.status_code,
                },
            )
            return False
        result = reconcile_positions(
            broker_positions,
            core.orchestrator.sessions,
        )
        await core.orchestrator.reconcile_complete(safe_for_new_entries=result.safe_for_new_entries)
        if result.safe_for_new_entries:
            by_symbol = {item.symbol: item for item in broker_positions}
            for symbol, position in core.positions.items():
                broker_position = by_symbol.get(symbol)
                if broker_position is None:
                    continue
                position.sellable_quantity = broker_position.sellable_quantity
                position.average_entry_price = broker_position.average_price
                session = core.orchestrator.sessions[symbol]
                session.sellable_quantity = broker_position.sellable_quantity
        await repository.audit(
            "OPENING_ACCOUNT_RECONCILED",
            correlation_id=self.correlation_id,
            payload={
                "safe_for_new_entries": result.safe_for_new_entries,
                "issues": [
                    {
                        "symbol": issue.symbol,
                        "code": issue.code,
                        "broker_quantity": issue.broker_quantity,
                        "internal_quantity": issue.internal_quantity,
                    }
                    for issue in result.issues
                ],
            },
        )
        return result.safe_for_new_entries

    async def _poll_orders(
        self,
        core: TradingRuntimeCore,
        broker: KisClient,
        manager: OrderManager,
        journal: SqlOrderJournal,
        repository: SqlRuntimeRepository,
        buy_fill_notifications: asyncio.Queue[tuple[str, str, int, int]],
        stop_notifications: asyncio.Queue[tuple[str, str, int, Decimal, str]],
        selection_names: dict[str, str],
    ) -> None:
        interval = float(self.settings.order_poll_interval_seconds)
        backoff = interval
        while True:
            if not core.submitted:
                await asyncio.sleep(max(5.0, interval))
                continue
            trading_date = datetime.now(KST).strftime("%Y%m%d")
            try:
                statuses = await broker.daily_order_statuses(trading_date=trading_date)
            except KisApiError as exc:
                await repository.audit(
                    "KIS_ORDER_STATUS_POLL_ERROR",
                    correlation_id=self.correlation_id,
                    payload={
                        "error": type(exc).__name__,
                        "status_code": exc.status_code,
                        "retry_seconds": backoff,
                    },
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = interval
            for status in statuses:
                key = core.order_number_to_key.get(status.broker_order_no)
                if key is None:
                    continue
                submitted = core.submitted[key]
                if core.cancellation_required(status):
                    cancellation = await manager.cancel(
                        broker_order_no=status.broker_order_no,
                        branch_no=status.branch_no,
                        remaining_quantity=status.remaining_quantity,
                    )
                    core.record_cancellation_requested(status.broker_order_no)
                    await repository.audit(
                        "BUY_REMAINDER_CANCEL_SUBMITTED",
                        correlation_id=self.correlation_id,
                        payload={
                            "symbol": status.symbol,
                            "original_order_no": status.broker_order_no,
                            "cancellation_order_no": cancellation.cancellation_order_no,
                            "remaining_quantity": status.remaining_quantity,
                        },
                    )
                position_before = core.positions.get(status.symbol)
                generation = submitted.intent.generation
                delta = core.apply_order_status(status, observed_at=datetime.now(UTC))
                persisted_delta = await journal.apply_broker_status(
                    idempotency_key=submitted.intent.idempotency_key,
                    broker_order_no=status.broker_order_no,
                    symbol=status.symbol,
                    ordered_quantity=status.ordered_quantity,
                    cumulative_filled_quantity=status.filled_quantity,
                    average_fill_price=status.average_fill_price,
                    remaining_quantity=status.remaining_quantity,
                    filled_at=datetime.now(UTC),
                )
                if persisted_delta != delta:
                    await repository.audit(
                        "FILL_LEDGER_DELTA_MISMATCH",
                        correlation_id=self.correlation_id,
                        payload={
                            "symbol": status.symbol,
                            "runtime_delta": delta,
                            "ledger_delta": persisted_delta,
                            "intent_key": submitted.intent.idempotency_key,
                        },
                    )
                if delta > 0:
                    await self._queue_buy_fill_notification(
                        submitted.intent,
                        status,
                        buy_fill_notifications,
                        selection_names,
                        repository,
                    )
                    await self._queue_stop_loss_notification(
                        submitted.intent,
                        status,
                        position_before,
                        stop_notifications,
                        selection_names,
                        repository,
                    )
                if submitted.intent.side.value == "BUY":
                    if (
                        delta > 0
                        and submitted.intent.idempotency_key not in core.recovered_order_keys
                    ):
                        reservation_id = f"{submitted.intent.idempotency_key}:CAPITAL"
                        await core.orchestrator.capital_allocator.consume_partial(
                            reservation_id,
                            amount=submitted.last_fill_value,
                        )
                        if status.remaining_quantity == 0:
                            await core.orchestrator.capital_allocator.release(reservation_id)
                    cancellation_finalized = await core.finalize_buy_cancellation(status)
                    if cancellation_finalized:
                        await repository.audit(
                            "BUY_REMAINDER_CANCEL_CONFIRMED",
                            correlation_id=self.correlation_id,
                            payload={
                                "symbol": status.symbol,
                                "original_order_no": status.broker_order_no,
                                "filled_quantity": status.filled_quantity,
                            },
                        )
                if delta <= 0:
                    if status.remaining_quantity == 0:
                        core.order_number_to_key.pop(status.broker_order_no, None)
                        core.submitted.pop(key, None)
                        core.recovered_order_keys.discard(key)
                    continue
                position = core.positions.get(status.symbol)
                if position is None:
                    await repository.close_position(
                        symbol=status.symbol,
                        generation=generation,
                    )
                else:
                    await repository.save_position(position)
                await repository.audit(
                    "ORDER_FILL_APPLIED",
                    correlation_id=self.correlation_id,
                    payload={
                        "symbol": status.symbol,
                        "side": submitted.intent.side.value,
                        "delta_quantity": delta,
                        "cumulative_quantity": status.filled_quantity,
                        "remaining_quantity": status.remaining_quantity,
                        "had_position_before": position_before is not None,
                    },
                )
                if status.remaining_quantity == 0:
                    core.order_number_to_key.pop(status.broker_order_no, None)
                    core.submitted.pop(key, None)
                    core.recovered_order_keys.discard(key)
            await asyncio.sleep(interval)

    async def _hard_stop_watchdog(
        self,
        core: TradingRuntimeCore,
        broker: KisClient,
        repository: SqlRuntimeRepository,
    ) -> None:
        persisted_peaks = {
            symbol: position.peak_return_pct for symbol, position in core.positions.items()
        }
        while True:
            if trading_session_phase(datetime.now(UTC)) is not TradingSessionPhase.KRX_REGULAR:
                await asyncio.sleep(30.0)
                continue
            for symbol in list(core.positions):
                try:
                    quote = await broker.current_price(symbol)
                    intent = await core.process_watchdog_price(
                        symbol=symbol,
                        price=quote.price,
                        observed_at=datetime.now(UTC),
                    )
                    if intent is not None:
                        await repository.audit(
                            "REST_HARD_STOP_ENQUEUED",
                            correlation_id=self.correlation_id,
                            payload={
                                "symbol": symbol,
                                "price": quote.price,
                                "intent_key": intent.idempotency_key,
                            },
                        )
                    position = core.positions.get(symbol)
                    if (
                        position is not None
                        and persisted_peaks.get(symbol) != position.peak_return_pct
                    ):
                        await repository.save_position(position)
                        persisted_peaks[symbol] = position.peak_return_pct
                except Exception as exc:
                    await repository.audit(
                        "REST_HARD_STOP_WATCHDOG_ERROR",
                        correlation_id=self.correlation_id,
                        payload={"symbol": symbol, "error": type(exc).__name__},
                    )
            await asyncio.sleep(float(self.settings.order_poll_interval_seconds))


class _KisOrderBrokerAdapter:
    def __init__(self, client: KisClient) -> None:
        self._client = client

    async def submit_cash_order(
        self,
        *,
        side: str,
        symbol: str,
        quantity: int,
        order_type: str,
        limit_price: int | None = None,
    ) -> BrokerReceipt:
        receipt = await self._client.submit_cash_order(
            side=side,
            symbol=symbol,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
        )
        return BrokerReceipt(receipt.broker_order_no, receipt.order_time)

    async def cancel_cash_order(
        self,
        *,
        broker_order_no: str,
        branch_no: str,
        quantity: int,
    ) -> BrokerReceipt:
        receipt = await self._client.cancel_cash_order(
            broker_order_no=broker_order_no,
            branch_no=branch_no,
            quantity=quantity,
        )
        return BrokerReceipt(receipt.broker_order_no, receipt.order_time)


# Archived test/import compatibility. The active composition root is TradingApplication.
PaperTradingApplication = TradingApplication
