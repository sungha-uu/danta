from __future__ import annotations

from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


class TradingSessionPhase(StrEnum):
    DORMANT = "DORMANT"
    NXT_PREMARKET = "NXT_PREMARKET"
    NXT_WITH_KRX_EXPECTED = "NXT_WITH_KRX_EXPECTED"
    OPENING_PLAN_LOCKED = "OPENING_PLAN_LOCKED"
    KRX_REGULAR = "KRX_REGULAR"
    NXT_AFTERMARKET = "NXT_AFTERMARKET"


NXT_OPEN = time(8, 0)
KRX_EXPECTED_OPEN = time(8, 30)
OPENING_PLAN_LOCK = time(8, 50)
KRX_OPEN = time(9, 0)
KRX_CLOSE = time(15, 30)
NXT_CLOSE = time(20, 0)
KRX_REGULAR_MINUTES_PER_DAY = 390


def trading_session_phase(at: datetime) -> TradingSessionPhase:
    """Return the single runtime phase for a timezone-aware timestamp."""
    local = _kst(at)
    if local.weekday() >= 5:
        return TradingSessionPhase.DORMANT
    current = local.time()
    if NXT_OPEN <= current < KRX_EXPECTED_OPEN:
        return TradingSessionPhase.NXT_PREMARKET
    if KRX_EXPECTED_OPEN <= current < OPENING_PLAN_LOCK:
        return TradingSessionPhase.NXT_WITH_KRX_EXPECTED
    if OPENING_PLAN_LOCK <= current < KRX_OPEN:
        return TradingSessionPhase.OPENING_PLAN_LOCKED
    if KRX_OPEN <= current < KRX_CLOSE:
        return TradingSessionPhase.KRX_REGULAR
    if KRX_CLOSE <= current < NXT_CLOSE:
        return TradingSessionPhase.NXT_AFTERMARKET
    return TradingSessionPhase.DORMANT


def seconds_until_phase_change(at: datetime) -> float:
    """Return a positive delay to the next weekday session boundary."""
    local = _kst(at)
    boundaries = (
        NXT_OPEN,
        KRX_EXPECTED_OPEN,
        OPENING_PLAN_LOCK,
        KRX_OPEN,
        KRX_CLOSE,
        NXT_CLOSE,
    )
    for day_offset in range(0, 9):
        candidate_date = local.date() + timedelta(days=day_offset)
        if candidate_date.weekday() >= 5:
            continue
        for boundary in boundaries:
            candidate = datetime.combine(candidate_date, boundary, tzinfo=KST)
            delta = (candidate - local).total_seconds()
            if delta > 0:
                return delta
    raise RuntimeError("could not find the next trading session boundary")


def krx_regular_trading_minutes_between(start: datetime, end: datetime) -> int:
    """Count elapsed KRX regular-session minutes, excluding nights and weekends.

    This deterministic order-path clock deliberately makes no network call.
    Exchange holidays require a versioned local calendar before live promotion;
    the current paper policy covers closed hours and the reported weekend failure.
    """
    local_start = _kst(start)
    local_end = _kst(end)
    if local_end <= local_start:
        return 0
    total_seconds = 0.0
    day_count = (local_end.date() - local_start.date()).days
    for day_offset in range(day_count + 1):
        trading_date = local_start.date() + timedelta(days=day_offset)
        if trading_date.weekday() >= 5:
            continue
        session_start = datetime.combine(trading_date, KRX_OPEN, tzinfo=KST)
        session_end = datetime.combine(trading_date, KRX_CLOSE, tzinfo=KST)
        overlap_start = max(local_start, session_start)
        overlap_end = min(local_end, session_end)
        if overlap_end > overlap_start:
            total_seconds += (overlap_end - overlap_start).total_seconds()
    return int(total_seconds // 60)


def _kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(KST)
