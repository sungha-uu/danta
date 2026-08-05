from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from danta.services.market_session import (
    TradingSessionPhase,
    krx_regular_trading_minutes_between,
    seconds_until_phase_change,
    trading_session_phase,
)

KST = ZoneInfo("Asia/Seoul")


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (7, 59, TradingSessionPhase.DORMANT),
        (8, 0, TradingSessionPhase.NXT_PREMARKET),
        (8, 30, TradingSessionPhase.NXT_WITH_KRX_EXPECTED),
        (8, 50, TradingSessionPhase.OPENING_PLAN_LOCKED),
        (9, 0, TradingSessionPhase.KRX_REGULAR),
        (15, 30, TradingSessionPhase.NXT_AFTERMARKET),
        (20, 0, TradingSessionPhase.DORMANT),
    ],
)
def test_weekday_session_boundaries(hour: int, minute: int, expected: TradingSessionPhase) -> None:
    assert trading_session_phase(datetime(2026, 7, 30, hour, minute, tzinfo=KST)) is expected


def test_weekend_is_dormant() -> None:
    assert (
        trading_session_phase(datetime(2026, 8, 1, 10, 0, tzinfo=KST))
        is TradingSessionPhase.DORMANT
    )


def test_next_boundary_during_regular_session_is_close() -> None:
    seconds = seconds_until_phase_change(datetime(2026, 7, 30, 15, 29, tzinfo=KST))
    assert seconds == 60


def test_session_clock_requires_timezone() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        trading_session_phase(datetime(2026, 7, 30, 9, 0))


def test_holding_clock_excludes_weekend_and_closed_hours() -> None:
    friday_open = datetime(2026, 7, 31, 9, 0, tzinfo=KST)
    monday_open = datetime(2026, 8, 3, 9, 0, tzinfo=KST)
    monday_close = datetime(2026, 8, 3, 15, 30, tzinfo=KST)

    assert krx_regular_trading_minutes_between(friday_open, monday_open) == 390
    assert krx_regular_trading_minutes_between(friday_open, monday_close) == 780


def test_holding_clock_counts_only_overlap_inside_regular_session() -> None:
    before_open = datetime(2026, 7, 30, 8, 0, tzinfo=KST)
    after_close = datetime(2026, 7, 30, 20, 0, tzinfo=KST)

    assert krx_regular_trading_minutes_between(before_open, after_close) == 390


def test_holding_clock_rejects_naive_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        krx_regular_trading_minutes_between(
            datetime(2026, 7, 31, 9, 0),
            datetime(2026, 8, 3, 9, 0, tzinfo=KST),
        )
