from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from danta.domain.entry import EntryDecision, EntryPolicy, evaluate_entry
from danta.domain.market import MarketSnapshot
from danta.domain.risk import ExitDecision, ExitPolicy, PositionRiskSnapshot, evaluate_exit


@dataclass(frozen=True, slots=True)
class EntryEvaluation:
    mandate_id: str
    snapshot: MarketSnapshot
    maximum_price: int
    policy: EntryPolicy
    now: datetime


@dataclass(frozen=True, slots=True)
class ExitEvaluation:
    snapshot: PositionRiskSnapshot
    policy: ExitPolicy


@dataclass(frozen=True, slots=True)
class StopActor:
    pass


ActorMessage = EntryEvaluation | ExitEvaluation | StopActor
EntrySink = Callable[[str, EntryDecision, datetime], Awaitable[None]]
ExitSink = Callable[[ExitDecision, datetime], Awaitable[None]]


class SymbolActor:
    def __init__(
        self,
        symbol: str,
        *,
        on_entry: EntrySink,
        on_exit: ExitSink,
        queue_size: int = 500,
    ) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size must be positive")
        self.symbol = symbol
        self.on_entry = on_entry
        self.on_exit = on_exit
        self.queue: asyncio.Queue[ActorMessage] = asyncio.Queue(maxsize=queue_size)
        self.processed_messages = 0
        self.last_error: str | None = None
        self.last_heartbeat: datetime | None = None

    async def send(self, message: ActorMessage) -> None:
        message_symbol = _message_symbol(message)
        if message_symbol is not None and message_symbol != self.symbol:
            raise ValueError("message symbol does not match actor")
        await self.queue.put(message)

    async def run(self) -> None:
        while True:
            message = await self.queue.get()
            try:
                if isinstance(message, StopActor):
                    return
                if isinstance(message, EntryEvaluation):
                    entry_decision = evaluate_entry(
                        message.snapshot,
                        maximum_price=message.maximum_price,
                        policy=message.policy,
                        snapshot_is_fresh=message.snapshot.is_fresh(
                            now=message.now,
                            max_age_seconds=message.policy.max_snapshot_age_seconds,
                        ),
                    )
                    await self.on_entry(message.mandate_id, entry_decision, message.now)
                    self.last_heartbeat = message.now
                else:
                    exit_decision = evaluate_exit(message.snapshot, policy=message.policy)
                    await self.on_exit(exit_decision, message.snapshot.observed_at)
                    self.last_heartbeat = message.snapshot.observed_at
                self.processed_messages += 1
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
                raise
            finally:
                self.queue.task_done()


def _message_symbol(message: ActorMessage) -> str | None:
    if isinstance(message, EntryEvaluation):
        return message.snapshot.symbol
    if isinstance(message, ExitEvaluation):
        return message.snapshot.symbol
    return None


class SymbolSupervisor:
    def __init__(self) -> None:
        self.actors: dict[str, SymbolActor] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def start_actor(self, actor: SymbolActor) -> None:
        if actor.symbol in self.tasks:
            raise ValueError("symbol actor is already running")
        self.actors[actor.symbol] = actor
        self.tasks[actor.symbol] = asyncio.create_task(
            actor.run(), name=f"danta-symbol-{actor.symbol}"
        )

    async def stop_actor(self, symbol: str) -> None:
        actor = self.actors.get(symbol)
        task = self.tasks.get(symbol)
        if actor is None or task is None:
            return
        await actor.send(StopActor())
        await task
        del self.actors[symbol]
        del self.tasks[symbol]

    async def stop_all(self) -> None:
        for symbol in list(self.actors):
            await self.stop_actor(symbol)

    def failed(self) -> dict[str, BaseException]:
        result: dict[str, BaseException] = {}
        for symbol, task in self.tasks.items():
            if not task.done() or task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                result[symbol] = error
        return result
