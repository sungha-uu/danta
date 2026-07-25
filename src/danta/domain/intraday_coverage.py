from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class WindowCoverage:
    window_days: int
    completed_days: int
    structural_ready: bool

    @property
    def progress_label(self) -> str:
        return f"{self.completed_days}/{self.window_days}거래일"


@dataclass(frozen=True)
class IntradayCoveragePlan:
    symbol: str
    target_sessions: tuple[date, ...]
    completed_sessions: tuple[date, ...]
    missing_sessions: tuple[date, ...]
    windows: tuple[WindowCoverage, ...]

    @property
    def caught_up(self) -> bool:
        return not self.missing_sessions

    def window(self, days: int) -> WindowCoverage:
        for item in self.windows:
            if item.window_days == days:
                return item
        raise KeyError(days)


def plan_intraday_coverage(
    *,
    symbol: str,
    market_sessions_since_epoch: Sequence[date],
    stored_sessions: Iterable[date] = (),
    listing_date: date | None = None,
    windows: Sequence[int] = (7, 14, 21),
) -> IntradayCoveragePlan:
    """Plan missing one-minute trading sessions for an active universe member.

    ``market_sessions_since_epoch`` is the shared coverage range of the running
    system. A newly admitted symbol therefore catches up to the same range
    instead of receiving a fixed seven-day history.
    """

    normalized_symbol = symbol.strip()
    if not normalized_symbol:
        raise ValueError("symbol must not be blank")

    normalized_windows = tuple(dict.fromkeys(windows))
    if not normalized_windows or any(days <= 0 for days in normalized_windows):
        raise ValueError("windows must contain positive day counts")

    target_sessions = tuple(
        session
        for session in sorted(set(market_sessions_since_epoch))
        if listing_date is None or session >= listing_date
    )
    stored_set = set(stored_sessions)
    completed_sessions = tuple(session for session in target_sessions if session in stored_set)
    missing_sessions = tuple(session for session in target_sessions if session not in stored_set)

    window_coverage: list[WindowCoverage] = []
    for days in normalized_windows:
        required_sessions = target_sessions[-days:]
        completed_days = sum(session in stored_set for session in required_sessions)
        structural_ready = len(required_sessions) == days and completed_days == days
        window_coverage.append(
            WindowCoverage(
                window_days=days,
                completed_days=completed_days,
                structural_ready=structural_ready,
            )
        )

    return IntradayCoveragePlan(
        symbol=normalized_symbol,
        target_sessions=target_sessions,
        completed_sessions=completed_sessions,
        missing_sessions=missing_sessions,
        windows=tuple(window_coverage),
    )
