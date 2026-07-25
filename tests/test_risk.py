from decimal import Decimal

import pytest

from danta.domain.risk import (
    RiskObservation,
    hard_stop_price,
    hard_stop_triggered,
    weighted_average_fill,
)


def test_weighted_average_and_stop_price() -> None:
    average = weighted_average_fill([(100_000, 2), (101_000, 1)])
    assert average == Decimal("100333.3333333333333333333333")
    assert hard_stop_price(average, tick_size=100) == 93_300


@pytest.mark.parametrize(
    "observation",
    [
        RiskObservation(last_price=92_999),
        RiskObservation(last_price=94_000, best_bid=93_000),
        RiskObservation(last_price=94_000, broker_return_pct=Decimal("-7.0")),
    ],
)
def test_any_hard_stop_signal_triggers(observation: RiskObservation) -> None:
    assert hard_stop_triggered(93_000, observation) is True


def test_above_stop_does_not_trigger() -> None:
    observation = RiskObservation(
        last_price=93_100,
        best_bid=93_100,
        broker_return_pct=Decimal("-6.99"),
    )
    assert hard_stop_triggered(93_000, observation) is False

