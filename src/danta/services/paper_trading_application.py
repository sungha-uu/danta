from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text

from danta.adapters.kis.client import KisClient
from danta.adapters.kis.realtime import KisRealtimeClient
from danta.config import AppSettings, KisCredentials, TradingEnvironment
from danta.db.session import create_engine_and_session
from danta.domain.mandate import EntryMandate
from danta.domain.trading_session import SymbolSession, SymbolState
from danta.services.capital_allocator import CapitalAllocator
from danta.services.market_data_router import MarketDataRouter
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

    async def run(self) -> None:
        engine, session_factory = create_engine_and_session(self.settings.database_url)
        async with engine.connect() as connection:
            revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        if revision != "0002_execution_runtime":
            await engine.dispose()
            raise RuntimeError(
                "database schema is not current; run alembic upgrade head"
            )
        repository = SqlRuntimeRepository(session_factory)
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
            try:
                async with asyncio.TaskGroup() as group:
                    group.create_task(
                        self._consume_realtime(router, realtime, repository),
                        name="danta-kis-realtime",
                    )
                    group.create_task(pump.run(), name="danta-order-pump")
                    group.create_task(
                        self._poll_orders(core, broker, manager, repository),
                        name="danta-order-reconciliation",
                    )
                    group.create_task(
                        self._hard_stop_watchdog(core, broker, repository),
                        name="danta-rest-hard-stop-watchdog",
                    )
            finally:
                await router.stop()
                await realtime.close()
                await engine.dispose()

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
    ) -> None:
        interval = float(self.settings.order_poll_interval_seconds)
        while True:
            trading_date = datetime.now(KST).strftime("%Y%m%d")
            statuses = await broker.daily_order_statuses(trading_date=trading_date)
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
                if delta <= 0:
                    continue
                if submitted.intent.side.value == "BUY":
                    reservation_id = f"{submitted.intent.idempotency_key}:CAPITAL"
                    await core.orchestrator.capital_allocator.consume_partial(
                        reservation_id,
                        amount=submitted.last_fill_value,
                    )
                    if status.remaining_quantity == 0:
                        await core.orchestrator.capital_allocator.release(
                            reservation_id
                        )
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
