from __future__ import annotations

import asyncio
import itertools

from danta.domain.trading_session import OrderIntent


class PriorityIntentScheduler:
    def __init__(self, *, maxsize: int = 1000) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self._queue: asyncio.PriorityQueue[tuple[int, int, OrderIntent]] = (
            asyncio.PriorityQueue(maxsize=maxsize)
        )
        self._sequence = itertools.count()
        self._known_keys: set[str] = set()
        self._lock = asyncio.Lock()

    async def put(self, intent: OrderIntent) -> bool:
        async with self._lock:
            if intent.idempotency_key in self._known_keys:
                return False
            self._known_keys.add(intent.idempotency_key)
        try:
            await self._queue.put((int(intent.priority), next(self._sequence), intent))
        except BaseException:
            async with self._lock:
                self._known_keys.discard(intent.idempotency_key)
            raise
        return True

    async def get(self) -> OrderIntent:
        _, _, intent = await self._queue.get()
        return intent

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()

    async def forget(self, idempotency_key: str) -> None:
        async with self._lock:
            self._known_keys.discard(idempotency_key)

    @property
    def qsize(self) -> int:
        return self._queue.qsize()
