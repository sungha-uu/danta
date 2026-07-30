from __future__ import annotations

import asyncio
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
    OrderIntent,
    SymbolSession,
    SymbolState,
)
from danta.services.capital_allocator import CapitalAllocator
from danta.services.market_data_router import MarketDataRouter
from danta.services.market_guard import MarketGuardDecision
from danta.services.market_wide_monitor import (
    MarketStatusPublisher,
    MarketWideCollector,
    MarketWideMonitor,
)
from danta.services.market_wide_repository import MarketWideRepository
from danta.services.notifier import NotificationError, SmtpNotifier
from danta.services.order_manager import BrokerReceipt, OrderManager
from danta.services.policy_registry import TradingPolicyRegistry
from danta.services.priority_intent_scheduler import PriorityIntentScheduler
from danta.services.reconciliation import reconcile_positions
from danta.services.runtime_repository import SqlRuntimeRepository
from danta.services.sql_order_journal import SqlOrderJournal
from danta.services.trading_orchestrator import TradingOrchestrator
from danta.services.trading_runtime import ManagedPosition, OrderPump, TradingRuntimeCore

KST = ZoneInfo("Asia/Seoul")


class PaperTradingApplication:
    """KIS paper-only executable composition root."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        credentials: KisCredentials,
        mandate: EntryMandate,
        policies: TradingPolicyRegistry,
    ) -> None:
        if settings.environment is not TradingEnvironment.PAPER:
            raise PermissionError("paper runtime requires paper application settings")
        if credentials.environment is not TradingEnvironment.PAPER:
            raise PermissionError("paper runtime requires paper KIS credentials")
        if not settings.paper_order_execution_enabled:
            raise PermissionError("paper order execution is disabled in config")
        if not policies.entry.approved_for_paper or not policies.exit.approved_for_paper:
            raise PermissionError("entry and exit policies must be approved for paper")
        self.settings = settings
        self.credentials = credentials
        self.mandate = mandate
        self.policies = policies
        self._notified_price_intents: set[str] = set()
        self._notified_stop_intents: set[str] = set()

    async def run(self) -> None:
        engine, session_factory = create_engine_and_session(self.settings.database_url)
        async with engine.connect() as connection:
            revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        if revision != "0003_market_wide_monitor":
            await engine.dispose()
            raise RuntimeError(
                "database schema is not current; run alembic upgrade head"
            )
        repository = SqlRuntimeRepository(session_factory)
        market_repository = MarketWideRepository(session_factory)
        notifier: SmtpNotifier | None = None
        if self.settings.smtp_enabled:
            try:
                notifier = SmtpNotifier(load_smtp_config(self.settings))
            except (OSError, ValueError) as exc:
                await repository.audit(
                    "ENTRY_PRICE_EMAIL_CONFIGURATION_ERROR",
                    correlation_id=self.mandate.command_id,
                    payload={"error": type(exc).__name__},
                )
        price_notifications: asyncio.Queue[tuple[str, str, int]] = asyncio.Queue()
        stop_notifications: asyncio.Queue[
            tuple[str, str, int, Decimal]
        ] = asyncio.Queue()
        selection_names = {
            selection.symbol: selection.name
            for selection in self.mandate.selections
        }
        scheduler = PriorityIntentScheduler()
        orchestrator = TradingOrchestrator(
            capital_allocator=CapitalAllocator(),
            scheduler=scheduler,
            max_approved_symbols=self.settings.maximum_managed_symbols,
        )
        core = TradingRuntimeCore(
            orchestrator=orchestrator,
            entry_policy=self.policies.entry.to_domain(),
            exit_policy=self.policies.exit.to_domain(),
        )
        async with KisClient(
            self.credentials,
            token_cache_path=Path("data/kis-token-cache.json"),
            order_submission_enabled=True,
        ) as broker:
            broker_positions = await broker.positions()
            stored_positions = await repository.load_open_positions()
            broker_by_symbol = {position.symbol: position for position in broker_positions}
            for stored in stored_positions:
                broker_position = broker_by_symbol.get(stored.symbol)
                sellable = (
                    broker_position.sellable_quantity if broker_position is not None else 0
                )
                orchestrator.sessions[stored.symbol] = SymbolSession(
                    symbol=stored.symbol,
                    generation=stored.generation,
                    state=SymbolState.POSITION_OPEN,
                    quantity=stored.quantity,
                    sellable_quantity=sellable,
                )
            reconciliation = reconcile_positions(
                broker_positions, orchestrator.sessions
            )
            await orchestrator.reconcile_complete(
                safe_for_new_entries=reconciliation.safe_for_new_entries
            )
            await repository.audit(
                "STARTUP_RECONCILIATION",
                correlation_id=self.mandate.command_id,
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
                core.restore_position(
                    ManagedPosition(
                        symbol=stored.symbol,
                        generation=stored.generation,
                        quantity=stored.quantity,
                        sellable_quantity=(
                            broker_position.sellable_quantity
                            if broker_position is not None
                            else 0
                        ),
                        average_entry_price=stored.average_entry_price,
                        opened_at=stored.opened_at,
                    )
                )
            if reconciliation.safe_for_new_entries:
                latest_generations = await repository.latest_generations(
                    [selection.symbol for selection in self.mandate.selections]
                )
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
                await core.activate_mandate(self.mandate, orderable_cash=cash.amount)
                await repository.audit(
                    "MANDATE_ACTIVATED",
                    correlation_id=self.mandate.command_id,
                    payload={
                        "symbols": list(core.signals),
                        "orderable_cash": cash.amount,
                        "entry_policy": self.policies.entry.version,
                        "exit_policy": self.policies.exit.version,
                    },
                )
            elif not core.signals:
                raise RuntimeError(
                    "reconciliation failed and no internally managed position can be protected"
                )
            realtime = KisRealtimeClient(self.credentials)
            manager = OrderManager(
                _KisOrderBrokerAdapter(broker),
                SqlOrderJournal(session_factory),
            )
            pump = OrderPump(
                core=core,
                manager=manager,
                on_error=lambda intent, error: repository.audit(
                    "ORDER_SUBMISSION_ERROR",
                    correlation_id=self.mandate.command_id,
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
                    correlation_id=self.mandate.command_id,
                    payload={"symbol": symbol, "error": type(error).__name__},
                ),
            )
            router.start(core.signals)
            market_monitor = MarketWideMonitor(
                collector=MarketWideCollector(broker),
                repository=market_repository,
                on_transition=lambda snapshot, decision, previous: (
                    self._market_risk_transition(
                        snapshot,
                        decision,
                        previous,
                        notifier,
                        repository,
                    )
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
                        self._consume_realtime(router, realtime, repository),
                        name="danta-kis-realtime",
                    )
                    group.create_task(pump.run(), name="danta-order-pump")
                    group.create_task(
                        self._poll_orders(
                            core,
                            broker,
                            manager,
                            repository,
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
                await repository.audit(
                    "MARKET_WIDE_SNAPSHOT",
                    correlation_id=self.mandate.command_id,
                    payload={
                        "risk_level": decision.level.value,
                        "entry_guard": decision.risk.value,
                        "stress_score": str(decision.stress_score),
                        "kospi_return_pct": str(snapshot.kospi_return_pct),
                        "declining_issue_ratio": str(
                            snapshot.declining_issue_ratio
                        ),
                        "foreign_net_million": snapshot.investor.foreign,
                        "pension_net_million": (
                            snapshot.investor.pension_fund_etc
                        ),
                        "program_net_million": snapshot.program.total,
                    },
                )
                if publisher is not None and loop.time() >= next_publish_at:
                    try:
                        target = await publisher.publish(snapshot, decision)
                        await repository.audit(
                            "MARKET_STATUS_PUBLISHED",
                            correlation_id=self.mandate.command_id,
                            payload={"path": str(target)},
                        )
                    except Exception as exc:
                        await repository.audit(
                            "MARKET_STATUS_PUBLISH_ERROR",
                            correlation_id=self.mandate.command_id,
                            payload={"error": type(exc).__name__},
                        )
                    finally:
                        next_publish_at = loop.time() + publish_interval
            except Exception as exc:
                # A broken market-wide feed blocks only new entries. Position
                # monitoring and protective exits continue in their own tasks.
                core.set_market_guard(MarketRisk.RISK_OFF, stress_score=Decimal("1"))
                await repository.audit(
                    "MARKET_WIDE_MONITOR_ERROR",
                    correlation_id=self.mandate.command_id,
                    payload={"error": type(exc).__name__},
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
            correlation_id=self.mandate.command_id,
            payload={
                "previous": None if previous is None else previous.value,
                "current": decision.level.value,
                "reason_codes": list(decision.reason_codes),
            },
        )
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
                correlation_id=self.mandate.command_id,
                payload={"recipient_count": receipt.recipient_count},
            )
        except (NotificationError, OSError) as exc:
            await repository.audit(
                "MARKET_RISK_EMAIL_ERROR",
                correlation_id=self.mandate.command_id,
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
            correlation_id=self.mandate.command_id,
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
                            correlation_id=self.mandate.command_id,
                            payload={
                                "intent_key": intent_key,
                                "recipient_count": receipt.recipient_count,
                            },
                        )
                        break
                    except (NotificationError, OSError) as exc:
                        await repository.audit(
                            "ENTRY_LIMIT_PRICE_EMAIL_ERROR",
                            correlation_id=self.mandate.command_id,
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
        queue: asyncio.Queue[tuple[str, str, int, Decimal]],
        selection_names: dict[str, str],
        repository: SqlRuntimeRepository,
    ) -> None:
        if (
            intent.side is not IntentSide.SELL
            or intent.cause
            not in {
                "EARLY_DEFENSE",
                "HARD_DEFENSE_MINUS_5",
                "HARD_STOP_MINUS_7",
            }
            or status.remaining_quantity != 0
            or status.filled_quantity <= 0
            or position_before is None
            or intent.idempotency_key in self._notified_stop_intents
        ):
            return
        self._notified_stop_intents.add(intent.idempotency_key)
        return_pct = (
            (status.average_fill_price - position_before.average_entry_price)
            / position_before.average_entry_price
            * Decimal("100")
        )
        await queue.put(
            (
                intent.idempotency_key,
                selection_names.get(intent.symbol, intent.symbol),
                int(status.average_fill_price),
                return_pct,
            )
        )
        await repository.audit(
            "HARD_STOP_EMAIL_QUEUED",
            correlation_id=self.mandate.command_id,
            payload={
                "symbol": intent.symbol,
                "average_fill_price": int(status.average_fill_price),
                "return_pct": str(return_pct),
                "intent_key": intent.idempotency_key,
            },
        )

    async def _send_stop_loss_notifications(
        self,
        queue: asyncio.Queue[tuple[str, str, int, Decimal]],
        notifier: SmtpNotifier,
        repository: SqlRuntimeRepository,
    ) -> None:
        retry_delays = (2.0, 10.0)
        while True:
            intent_key, name, price, return_pct = await queue.get()
            try:
                for attempt in range(3):
                    try:
                        receipt = await asyncio.to_thread(
                            notifier.send_stop_loss_completed,
                            [(name, price, return_pct)],
                        )
                        await repository.audit(
                            "HARD_STOP_EMAIL_SENT",
                            correlation_id=self.mandate.command_id,
                            payload={
                                "intent_key": intent_key,
                                "recipient_count": receipt.recipient_count,
                            },
                        )
                        break
                    except (NotificationError, OSError) as exc:
                        await repository.audit(
                            "HARD_STOP_EMAIL_ERROR",
                            correlation_id=self.mandate.command_id,
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

    async def _consume_realtime(
        self,
        router: MarketDataRouter,
        realtime: KisRealtimeClient,
        repository: SqlRuntimeRepository,
    ) -> None:
        while True:
            try:
                async for event in realtime.stream(list(router.queues)):
                    await router.route(event)
            except Exception as exc:
                await repository.audit(
                    "KIS_REALTIME_STREAM_ERROR",
                    correlation_id=self.mandate.command_id,
                    payload={"error": type(exc).__name__},
                )
                await asyncio.sleep(5)

    async def _poll_orders(
        self,
        core: TradingRuntimeCore,
        broker: KisClient,
        manager: OrderManager,
        repository: SqlRuntimeRepository,
        stop_notifications: asyncio.Queue[
            tuple[str, str, int, Decimal]
        ],
        selection_names: dict[str, str],
    ) -> None:
        interval = float(self.settings.order_poll_interval_seconds)
        backoff = interval
        while True:
            trading_date = datetime.now(KST).strftime("%Y%m%d")
            try:
                statuses = await broker.daily_order_statuses(
                    trading_date=trading_date
                )
            except KisApiError as exc:
                await repository.audit(
                    "KIS_ORDER_STATUS_POLL_ERROR",
                    correlation_id=self.mandate.command_id,
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
                        correlation_id=self.mandate.command_id,
                        payload={
                            "symbol": status.symbol,
                            "original_order_no": status.broker_order_no,
                            "cancellation_order_no": cancellation.cancellation_order_no,
                            "remaining_quantity": status.remaining_quantity,
                        },
                    )
                position_before = core.positions.get(status.symbol)
                generation = submitted.intent.generation
                delta = core.apply_order_status(
                    status, observed_at=datetime.now(UTC)
                )
                if delta > 0:
                    await self._queue_stop_loss_notification(
                        submitted.intent,
                        status,
                        position_before,
                        stop_notifications,
                        selection_names,
                        repository,
                    )
                if submitted.intent.side.value == "BUY":
                    if delta > 0:
                        reservation_id = f"{submitted.intent.idempotency_key}:CAPITAL"
                        await core.orchestrator.capital_allocator.consume_partial(
                            reservation_id,
                            amount=submitted.last_fill_value,
                        )
                        if status.remaining_quantity == 0:
                            await core.orchestrator.capital_allocator.release(
                                reservation_id
                            )
                    cancellation_finalized = await core.finalize_buy_cancellation(
                        status
                    )
                    if cancellation_finalized:
                        await repository.audit(
                            "BUY_REMAINDER_CANCEL_CONFIRMED",
                            correlation_id=self.mandate.command_id,
                            payload={
                                "symbol": status.symbol,
                                "original_order_no": status.broker_order_no,
                                "filled_quantity": status.filled_quantity,
                            },
                        )
                if delta <= 0:
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
                    correlation_id=self.mandate.command_id,
                    payload={
                        "symbol": status.symbol,
                        "side": submitted.intent.side.value,
                        "delta_quantity": delta,
                        "cumulative_quantity": status.filled_quantity,
                        "remaining_quantity": status.remaining_quantity,
                        "had_position_before": position_before is not None,
                    },
                )
            await asyncio.sleep(interval)

    async def _hard_stop_watchdog(
        self,
        core: TradingRuntimeCore,
        broker: KisClient,
        repository: SqlRuntimeRepository,
    ) -> None:
        while True:
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
                            correlation_id=self.mandate.command_id,
                            payload={
                                "symbol": symbol,
                                "price": quote.price,
                                "intent_key": intent.idempotency_key,
                            },
                        )
                except Exception as exc:
                    await repository.audit(
                        "REST_HARD_STOP_WATCHDOG_ERROR",
                        correlation_id=self.mandate.command_id,
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
