from __future__ import annotations

from datetime import date, timedelta

import pytest

from danta.domain.intraday_coverage import plan_intraday_coverage


def _sessions(count: int) -> list[date]:
    start = date(2026, 7, 1)
    return [start + timedelta(days=index) for index in range(count)]


def test_initial_symbol_plans_seven_day_backfill() -> None:
    sessions = _sessions(7)

    plan = plan_intraday_coverage(symbol="005930", market_sessions_since_epoch=sessions)

    assert plan.missing_sessions == tuple(sessions)
    assert plan.window(7).progress_label == "0/7거래일"
    assert plan.window(7).structural_ready is False


def test_new_member_catches_up_to_current_ten_day_epoch() -> None:
    sessions = _sessions(10)

    plan = plan_intraday_coverage(symbol="000660", market_sessions_since_epoch=sessions)

    assert len(plan.missing_sessions) == 10
    assert plan.target_sessions[0] == sessions[0]
    assert plan.target_sessions[-1] == sessions[-1]


def test_existing_sessions_are_reused_and_only_gaps_are_requested() -> None:
    sessions = _sessions(10)
    stored = sessions[:7] + sessions[8:9]

    plan = plan_intraday_coverage(
        symbol="005380",
        market_sessions_since_epoch=sessions,
        stored_sessions=stored,
    )

    assert plan.missing_sessions == (sessions[7], sessions[9])
    assert plan.caught_up is False
    assert plan.window(7).completed_days == 5


def test_window_readiness_is_independent_for_seven_fourteen_and_twenty_one_days() -> None:
    sessions = _sessions(14)

    plan = plan_intraday_coverage(
        symbol="034020",
        market_sessions_since_epoch=sessions,
        stored_sessions=sessions,
    )

    assert plan.caught_up is True
    assert plan.window(7).structural_ready is True
    assert plan.window(14).structural_ready is True
    assert plan.window(21).progress_label == "14/21거래일"
    assert plan.window(21).structural_ready is False


def test_new_listing_never_requests_sessions_before_listing_date() -> None:
    sessions = _sessions(10)
    listing_date = sessions[6]

    plan = plan_intraday_coverage(
        symbol="123456",
        market_sessions_since_epoch=sessions,
        listing_date=listing_date,
    )

    assert plan.target_sessions == tuple(sessions[6:])
    assert plan.window(7).progress_label == "0/7거래일"


def test_invalid_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="symbol"):
        plan_intraday_coverage(symbol=" ", market_sessions_since_epoch=[])
    with pytest.raises(ValueError, match="windows"):
        plan_intraday_coverage(
            symbol="005930",
            market_sessions_since_epoch=[],
            windows=(7, 0),
        )
