from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol

from danta.adapters.kis.realtime import (
    KisRealtimeClient,
    MarketVenue,
    RealtimeEvent,
)
from danta.domain.premarket import PremarketPolicy
from danta.services.market_data_router import MarketDataRouter
from danta.services.market_session import (
    KST,
    TradingSessionPhase,
    seconds_until_phase_change,
    trading_session_phase,
)
from danta.services.overnight_guardian import (
    OvernightPosition,
    OvernightProtectionCoordinator,
    release_opening_plans_to_orchestrator,
)
from danta.services.trading_runtime import TradingRuntimeCore


class AuditRepository(Protocol):
    async def audit(
        self,
        event_type: str,
        *,
        correlation_id: str | None,
        payload: dict[str, object],
    ) -> None: ...


OpeningReconcile = Callable[[], Awaitable[bool]]
Clock = Callable[[], datetime]


class UnifiedTradingMonitor:
    """One long-running KRX/NXT session supervisor for a trading runtime."""

    def __init__(
        self,
        *,
        realtime: KisRealtimeClient,
        router: MarketDataRouter,
        core: TradingRuntimeCore,
        premarket_policy: PremarketPolicy,
        repository: AuditRepository,
        correlation_id: str,
        opening_reconcile: OpeningReconcile,
        clock: Clock | None = None,
    ) -> None:
        self.realtime = realtime
        self.router = router
        self.core = core
        self.premarket_policy = premarket_policy
        self.repository = repository
        self.correlation_id = correlation_id
        self.opening_reconcile = opening_reconcile
        self.clock = clock or (lambda: datetime.now(UTC))
        self.phase: TradingSessionPhase | None = None
        self.coordinator: OvernightProtectionCoordinator | None = None
        self._position_signature: tuple[tuple[object, ...], ...] = ()
        self._plan_locked_date: str | None = None
        self._plan_released_date: str | None = None

    async def run(self) -> None:
        while True:
            now = self.clock()
            phase = trading_session_phase(now)
            try:
                if phase is not self.phase:
                    await self._transition(phase, now)
                await self._run_phase(phase, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._audit(
                    "UNIFIED_MARKET_MONITOR_ERROR",
                    {
                        "phase": phase.value,
                        "error": type(exc).__name__,
                        "detail": str(exc)[:240],
                        "causes": _exception_causes(exc),
                    },
                )
                await asyncio.sleep(5)

    async def _transition(self, phase: TradingSessionPhase, now: datetime) -> None:
        previous = self.phase
        self.phase = phase
        await self._sync_coordinator()
        if phase is TradingSessionPhase.KRX_REGULAR:
            self.core.reset_market_signals()
            await self._release_opening_plans(now)
        await self._audit(
            "TRADING_SESSION_TRANSITION",
            {
                "previous": None if previous is None else previous.value,
                "current": phase.value,
                "observed_at": now.isoformat(),
            },
        )

    async def _run_phase(self, phase: TradingSessionPhase, now: datetime) -> None:
        timeout = max(0.1, seconds_until_phase_change(now))
        if phase is TradingSessionPhase.DORMANT:
            await asyncio.sleep(min(timeout, 60.0))
            return
        if phase is TradingSessionPhase.KRX_REGULAR:
            await self._consume_with_timeout(self._consume_krx(), timeout=timeout)
            return
        await self._sync_coordinator()
        if phase is TradingSessionPhase.OPENING_PLAN_LOCKED:
            await self._lock_opening_plans(now)
        if phase in {
            TradingSessionPhase.NXT_WITH_KRX_EXPECTED,
            TradingSessionPhase.OPENING_PLAN_LOCKED,
        }:
            await self._consume_with_timeout(self._consume_nxt_and_expected(), timeout=timeout)
            return
        await self._consume_with_timeout(self._consume_nxt(), timeout=timeout)

    async def _consume_with_timeout(self, awaitable: Awaitable[None], *, timeout: float) -> None:
        if not self.router.queues:
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
            self.router.subscriptions_changed.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self.router.subscriptions_changed.wait(),
                    timeout=min(timeout, 60.0),
                )
            return
        self.router.subscriptions_changed.clear()
        feed: asyncio.Future[None] = asyncio.ensure_future(awaitable)
        changed: asyncio.Task[None] = asyncio.create_task(
            _wait_event(self.router.subscriptions_changed)
        )
        try:
            done, _ = await asyncio.wait(
                {feed, changed},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task is feed:
                    task.result()
        finally:
            for task in (feed, changed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(feed, changed, return_exceptions=True)

    async def _consume_krx(self) -> None:
        async for event in self.realtime.stream(list(self.router.queues), venue=MarketVenue.KRX):
            await self.router.route(event)

    async def _consume_nxt(self) -> None:
        async for event in self.realtime.stream(list(self.router.queues), venue=MarketVenue.NXT):
            self._observe_outside_regular_session(event)

    async def _consume_nxt_and_expected(self) -> None:
        async for event in self.realtime.stream_premarket(list(self.router.queues)):
            self._observe_outside_regular_session(event)

    def _observe_outside_regular_session(self, event: RealtimeEvent) -> None:
        coordinator = self.coordinator
        if coordinator is not None:
            coordinator.process_event(event)

    async def _sync_coordinator(self) -> None:
        positions = [
            OvernightPosition(
                symbol=position.symbol,
                generation=position.generation,
                average_entry_price=position.average_entry_price,
                sellable_quantity=position.sellable_quantity,
            )
            for position in self.core.positions.values()
            if position.quantity > 0
        ]
        signature = tuple(
            sorted(
                (
                    item.symbol,
                    item.generation,
                    item.average_entry_price,
                    item.sellable_quantity,
                )
                for item in positions
            )
        )
        if signature == self._position_signature:
            return
        self._position_signature = signature
        self.coordinator = (
            OvernightProtectionCoordinator(
                positions,
                policy=self.premarket_policy,
            )
            if positions
            else None
        )
        await self._audit(
            "OVERNIGHT_POSITION_SET_SYNCED",
            {"symbols": [item.symbol for item in positions]},
        )

    async def _lock_opening_plans(self, now: datetime) -> None:
        local_date = now.astimezone(KST).date().isoformat()
        if self._plan_locked_date == local_date or self.coordinator is None:
            return
        plans = self.coordinator.lock_opening_plans(
            now=now,
            market_risk=self.core.market_risk,
            market_stress_score=self.core.market_stress_score,
        )
        self._plan_locked_date = local_date
        await self._audit(
            "OPENING_PROTECTION_PLANS_LOCKED",
            {
                "count": len(plans),
                "symbols": [plan.decision.symbol for plan in plans],
            },
        )

    async def _release_opening_plans(self, now: datetime) -> None:
        local = now.astimezone(KST)
        local_date = local.date().isoformat()
        if self._plan_released_date == local_date or self.coordinator is None:
            return
        self._plan_released_date = local_date
        if not self.coordinator.plans:
            return
        if not (local.hour == 9 and local.minute <= 5):
            await self._audit(
                "OPENING_PROTECTION_RELEASE_EXPIRED",
                {"observed_at": now.isoformat()},
            )
            return
        if not await self.opening_reconcile():
            await self._audit(
                "OPENING_PROTECTION_RELEASE_BLOCKED",
                {"reason": "ACCOUNT_RECONCILIATION_FAILED"},
            )
            return
        intents = await release_opening_plans_to_orchestrator(
            self.coordinator,
            self.core.orchestrator,
            now=now,
        )
        await self._audit(
            "OPENING_PROTECTION_PLANS_RELEASED",
            {
                "count": len(intents),
                "symbols": [intent.symbol for intent in intents],
            },
        )

    async def _audit(self, event_type: str, payload: dict[str, object]) -> None:
        await self.repository.audit(
            event_type,
            correlation_id=self.correlation_id,
            payload=payload,
        )


async def _wait_event(event: asyncio.Event) -> None:
    await event.wait()


def _exception_causes(exc: BaseException) -> list[str]:
    if isinstance(exc, BaseExceptionGroup):
        causes: list[str] = []
        for nested in exc.exceptions:
            causes.extend(_exception_causes(nested))
        return causes[:8]
    detail = str(exc).strip() or "no detail"
    return [f"{type(exc).__name__}: {detail}"[:320]]
