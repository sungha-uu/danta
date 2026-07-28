from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable

from danta.adapters.kis.realtime import RealtimeEvent
from danta.services.trading_runtime import TradingRuntimeCore

RouterErrorSink = Callable[[str, BaseException], Awaitable[None]]


class MarketDataRouter:
    """Bounded per-symbol mailboxes so one symbol cannot mutate another's state."""

    def __init__(
        self,
        core: TradingRuntimeCore,
        *,
        queue_size: int = 1000,
        on_error: RouterErrorSink | None = None,
    ) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size must be positive")
        self.core = core
        self.queue_size = queue_size
        self.on_error = on_error
        self.queues: dict[str, asyncio.Queue[RealtimeEvent | None]] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.dropped_events: dict[str, int] = {}

    def start(self, symbols: Iterable[str]) -> None:
        for symbol in dict.fromkeys(symbols):
            if symbol in self.tasks:
                continue
            queue: asyncio.Queue[RealtimeEvent | None] = asyncio.Queue(
                maxsize=self.queue_size
            )
            self.queues[symbol] = queue
            self.dropped_events[symbol] = 0
            self.tasks[symbol] = asyncio.create_task(
                self._run_symbol(symbol, queue),
                name=f"danta-market-router-{symbol}",
            )

    async def route(self, event: RealtimeEvent) -> bool:
        queue = self.queues.get(event.symbol)
        if queue is None:
            return False
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_events[event.symbol] += 1
            if self.on_error is not None:
                await self.on_error(
                    event.symbol,
                    RuntimeError("symbol market-data mailbox is full"),
                )
            return False
        return True

    async def stop(self) -> None:
        for queue in self.queues.values():
            await queue.put(None)
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.queues.clear()
        self.tasks.clear()

    async def _run_symbol(
        self,
        symbol: str,
        queue: asyncio.Queue[RealtimeEvent | None],
    ) -> None:
        while True:
            event = await queue.get()
            try:
                if event is None:
                    return
                await self.core.process_event(event)
            except Exception as exc:
                if self.on_error is not None:
                    await self.on_error(symbol, exc)
            finally:
                queue.task_done()
