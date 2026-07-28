from datetime import UTC, datetime
from decimal import Decimal

from danta.domain.entry import EntryAction, EntryPolicy
from danta.domain.market import MarketRisk, MarketSnapshot
from danta.domain.risk import ExitDecision
from danta.services.symbol_actor import EntryEvaluation, SymbolActor, SymbolSupervisor


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="005930",
        observed_at=datetime.now(UTC),
        last_price=70000,
        best_bid=69900,
        best_ask=70000,
        sell_pressure_score=Decimal("0.2"),
        stabilization_score=Decimal("0.9"),
        buy_recovery_score=Decimal("0.9"),
        weakness_score=Decimal("0.1"),
        market_stress_score=Decimal("0.1"),
        market_risk=MarketRisk.NORMAL,
    )


async def test_symbol_actor_serializes_entry_evaluations() -> None:
    actions: list[EntryAction] = []

    async def on_entry(
        _: str, decision: object, __: datetime
    ) -> None:
        actions.append(decision.action)  # type: ignore[attr-defined]

    async def on_exit(_: ExitDecision, __: datetime) -> None:
        raise AssertionError("exit callback should not run")

    actor = SymbolActor("005930", on_entry=on_entry, on_exit=on_exit)
    supervisor = SymbolSupervisor()
    supervisor.start_actor(actor)
    policy = EntryPolicy(
        version="entry-test",
        approved=True,
        max_snapshot_age_seconds=5,
        sell_pressure_block=Decimal("0.8"),
        stabilization_required=Decimal("0.6"),
        buy_recovery_required=Decimal("0.6"),
        max_spread_bps=Decimal("30"),
    )
    now = datetime.now(UTC)
    await actor.send(EntryEvaluation("m1", _snapshot(), 70000, policy, now))
    await actor.send(EntryEvaluation("m1", _snapshot(), 70000, policy, now))
    await actor.queue.join()
    await supervisor.stop_all()
    assert actions == [EntryAction.SUBMIT_LIMIT_BUY, EntryAction.SUBMIT_LIMIT_BUY]
    assert actor.processed_messages == 2
