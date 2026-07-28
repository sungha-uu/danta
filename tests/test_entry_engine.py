from datetime import UTC, datetime
from decimal import Decimal

from danta.domain.entry import EntryAction, EntryPolicy, evaluate_entry
from danta.domain.market import MarketRisk, MarketSnapshot


def _snapshot(**overrides: object) -> MarketSnapshot:
    values: dict[str, object] = {
        "symbol": "005930",
        "observed_at": datetime.now(UTC),
        "last_price": 70000,
        "best_bid": 69900,
        "best_ask": 70000,
        "sell_pressure_score": Decimal("0.2"),
        "stabilization_score": Decimal("0.8"),
        "buy_recovery_score": Decimal("0.8"),
        "weakness_score": Decimal("0.2"),
        "market_stress_score": Decimal("0.1"),
        "market_risk": MarketRisk.NORMAL,
        "box_valid": True,
        "data_fresh": True,
    }
    values.update(overrides)
    return MarketSnapshot(**values)  # type: ignore[arg-type]


def _policy(*, approved: bool = True) -> EntryPolicy:
    return EntryPolicy(
        version="entry-test-v1",
        approved=approved,
        max_snapshot_age_seconds=5,
        sell_pressure_block=Decimal("0.7"),
        stabilization_required=Decimal("0.6"),
        buy_recovery_required=Decimal("0.6"),
        max_spread_bps=Decimal("30"),
    )


def test_maximum_price_is_a_gate_not_an_immediate_market_order() -> None:
    waiting = evaluate_entry(
        _snapshot(best_ask=70100),
        maximum_price=70000,
        policy=_policy(),
        snapshot_is_fresh=True,
    )
    assert waiting.action is EntryAction.WAIT_PRICE

    ready = evaluate_entry(
        _snapshot(),
        maximum_price=70000,
        policy=_policy(),
        snapshot_is_fresh=True,
    )
    assert ready.action is EntryAction.SUBMIT_LIMIT_BUY
    assert ready.limit_price == 70000


def test_strong_sell_pressure_waits_even_below_user_maximum() -> None:
    decision = evaluate_entry(
        _snapshot(
            last_price=68000,
            best_bid=67900,
            best_ask=68000,
            sell_pressure_score=Decimal("0.9"),
        ),
        maximum_price=70000,
        policy=_policy(),
        snapshot_is_fresh=True,
    )
    assert decision.action is EntryAction.WAIT_SELL_PRESSURE
    assert decision.limit_price is None


def test_box_break_is_reference_only_for_entry() -> None:
    decision = evaluate_entry(
        _snapshot(last_price=65000, best_bid=64900, best_ask=65000, box_valid=False),
        maximum_price=70000,
        policy=_policy(),
        snapshot_is_fresh=True,
    )
    assert decision.action is EntryAction.SUBMIT_LIMIT_BUY
    assert decision.limit_price == 65000


def test_unapproved_policy_and_risk_off_block_entries() -> None:
    assert (
        evaluate_entry(
            _snapshot(),
            maximum_price=70000,
            policy=_policy(approved=False),
            snapshot_is_fresh=True,
        ).action
        is EntryAction.BLOCK
    )
    assert (
        evaluate_entry(
            _snapshot(market_risk=MarketRisk.RISK_OFF),
            maximum_price=70000,
            policy=_policy(),
            snapshot_is_fresh=True,
        ).action
        is EntryAction.BLOCK
    )
