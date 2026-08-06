from datetime import UTC, datetime
from decimal import Decimal

from danta.domain.market import MarketRisk
from danta.domain.risk import (
    ExitAction,
    ExitPolicy,
    ExitUrgency,
    PositionRiskSnapshot,
    evaluate_exit,
)


def _policy(*, approved: bool = True) -> ExitPolicy:
    return ExitPolicy(
        version="exit-test-v1",
        approved=approved,
        early_loss_pct=Decimal("-3"),
        strong_loss_pct=Decimal("-5"),
        early_defense_score=Decimal("0.65"),
        strong_sell_pressure=Decimal("0.75"),
        panic_market_stress=Decimal("0.8"),
        profit_arm_pct=Decimal("5"),
        profit_giveback_pct=Decimal("2"),
        profit_weakness_score=Decimal("0.7"),
        max_holding_minutes=300,
    )


def _snapshot(**overrides: object) -> PositionRiskSnapshot:
    values: dict[str, object] = {
        "symbol": "005930",
        "generation": 1,
        "average_entry_price": Decimal("100000"),
        "quantity": 10,
        "sellable_quantity": 10,
        "last_price": 100000,
        "best_bid": 99900,
        "broker_return_pct": None,
        "peak_return_pct": Decimal("0"),
        "held_minutes": 10,
        "sell_pressure_score": Decimal("0.2"),
        "weakness_score": Decimal("0.2"),
        "market_stress_score": Decimal("0.2"),
        "market_risk": MarketRisk.NORMAL,
        "box_valid": True,
        "data_fresh": True,
        "observed_at": datetime.now(UTC),
    }
    values.update(overrides)
    return PositionRiskSnapshot(**values)  # type: ignore[arg-type]


def test_hard_stop_works_even_when_adaptive_policy_is_not_approved() -> None:
    decision = evaluate_exit(
        _snapshot(last_price=93000, best_bid=93000),
        policy=_policy(approved=False),
    )
    assert decision.action is ExitAction.SELL_MARKET
    assert decision.urgency is ExitUrgency.HARD_STOP
    assert decision.quantity == 10


def test_minus_five_exits_without_strong_recovery() -> None:
    decision = evaluate_exit(
        _snapshot(
            last_price=95000,
            best_bid=95000,
            sell_pressure_score=Decimal("0.1"),
            weakness_score=Decimal("0.1"),
            market_stress_score=Decimal("0"),
        ),
        policy=_policy(),
    )
    assert decision.action is ExitAction.SELL_MARKET
    assert decision.urgency is ExitUrgency.PROTECTIVE
    assert decision.quantity == 10
    assert decision.reason_codes == ("HARD_DEFENSE_MINUS_5",)


def test_minus_five_allows_only_bounded_strong_recovery_observation() -> None:
    observing = evaluate_exit(
        _snapshot(
            last_price=95000,
            best_bid=95000,
            buy_recovery_score=Decimal("0.8"),
            sell_pressure_score=Decimal("0.1"),
            weakness_score=Decimal("0.1"),
            strong_defense_elapsed_seconds=14,
        ),
        policy=_policy(),
    )
    assert observing.action is ExitAction.HOLD
    assert observing.reason_codes == ("MINUS_5_STRONG_RECOVERY_OBSERVATION",)

    expired = evaluate_exit(
        _snapshot(
            last_price=95000,
            best_bid=95000,
            buy_recovery_score=Decimal("0.8"),
            sell_pressure_score=Decimal("0.1"),
            weakness_score=Decimal("0.1"),
            strong_defense_elapsed_seconds=15,
        ),
        policy=_policy(),
    )
    assert expired.action is ExitAction.SELL_MARKET
    assert expired.reason_codes == ("HARD_DEFENSE_MINUS_5",)


def test_minus_five_recovery_exception_never_overrides_risk_off() -> None:
    decision = evaluate_exit(
        _snapshot(
            last_price=95000,
            best_bid=95000,
            buy_recovery_score=Decimal("0.9"),
            sell_pressure_score=Decimal("0.05"),
            weakness_score=Decimal("0.05"),
            market_risk=MarketRisk.RISK_OFF,
        ),
        policy=_policy(),
    )
    assert decision.action is ExitAction.SELL_MARKET
    assert decision.reason_codes == ("HARD_DEFENSE_MINUS_5",)


def test_minus_three_uses_local_pressure_when_market_stress_is_unavailable() -> None:
    decision = evaluate_exit(
        _snapshot(
            last_price=96900,
            best_bid=96900,
            sell_pressure_score=Decimal("0.8"),
            weakness_score=Decimal("0.7"),
            market_stress_score=Decimal("0"),
        ),
        policy=_policy(),
    )
    assert decision.action is ExitAction.SELL_MARKET
    assert decision.urgency is ExitUrgency.PROTECTIVE
    assert decision.reason_codes == ("EARLY_DEFENSE",)


def test_minus_three_holds_without_local_or_market_weakness() -> None:
    decision = evaluate_exit(
        _snapshot(
            last_price=96900,
            best_bid=96900,
            sell_pressure_score=Decimal("0.2"),
            weakness_score=Decimal("0.2"),
            market_stress_score=Decimal("0"),
        ),
        policy=_policy(),
    )
    assert decision.action is ExitAction.HOLD


def test_above_minus_three_does_not_use_early_defense() -> None:
    decision = evaluate_exit(
        _snapshot(
            last_price=97100,
            best_bid=97100,
            sell_pressure_score=Decimal("1"),
            weakness_score=Decimal("1"),
            market_stress_score=Decimal("1"),
            market_risk=MarketRisk.RISK_OFF,
        ),
        policy=_policy(),
    )
    assert decision.action is ExitAction.HOLD


def test_profit_floor_requires_peak_giveback_and_weakness() -> None:
    decision = evaluate_exit(
        _snapshot(
            last_price=105000,
            best_bid=105000,
            peak_return_pct=Decimal("8"),
            weakness_score=Decimal("0.8"),
        ),
        policy=_policy(),
    )
    assert decision.action is ExitAction.SELL_MARKET
    assert decision.reason_codes == ("ADAPTIVE_PROFIT_FLOOR",)


def test_profit_protection_arms_at_three_with_tighter_early_giveback() -> None:
    policy = _policy()
    policy = ExitPolicy(
        **{
            field: getattr(policy, field)
            for field in policy.__dataclass_fields__
            if field != "profit_arm_pct"
        },
        profit_arm_pct=Decimal("3"),
    )
    decision = evaluate_exit(
        _snapshot(
            last_price=102000,
            best_bid=102000,
            peak_return_pct=Decimal("3.2"),
            weakness_score=Decimal("0.8"),
        ),
        policy=policy,
    )
    assert decision.action is ExitAction.SELL_MARKET
    assert decision.reason_codes == ("ADAPTIVE_PROFIT_FLOOR",)


def test_no_approved_adaptive_policy_holds_above_hard_stop() -> None:
    decision = evaluate_exit(
        _snapshot(last_price=95000, best_bid=95000),
        policy=_policy(approved=False),
    )
    assert decision.action is ExitAction.HOLD
