from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import HttpUrl

from danta.adapters.kis.client import KisApiError, KisClient, KisMinuteBar
from danta.adapters.krx.client import MarketDataset
from danta.dashboard.models import (
    AiGrade,
    CandidateView,
    ChartBar,
    DashboardReport,
    FlowBreakdown,
    WindowMetrics,
)
from danta.services.candidate_report import CandidateReportError

KST = timezone(timedelta(hours=9))
HUNDRED = Decimal("100")
WindowDays = Literal[7, 14, 21]
WINDOW_DAYS: tuple[WindowDays, WindowDays, WindowDays] = (7, 14, 21)
MIN_CAPITALIZATION = Decimal("500000000000")
MIN_PRICE = Decimal("5000")
MIN_AVERAGE_TRADING_VALUE = Decimal("5000000000")
EXCLUDED_NAME = re.compile(r"(?:우|우B|우C|스팩)\d*$")


@dataclass(frozen=True, slots=True)
class PrefilterCandidate:
    symbol: str
    name: str
    market_cap: Decimal
    latest_price: Decimal
    average_trading_value: Decimal


@dataclass(frozen=True, slots=True)
class HourBar:
    trading_date: str
    bucket: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class _Analyzed:
    symbol: str
    low: Decimal
    high: Decimal
    amplitude: Decimal
    position: Decimal
    target_price: Decimal
    lower_contacts: int
    target_reaches: int
    target_pending: int
    median_daily_range: Decimal
    max_daily_range: Decimal
    median_daily_rebound: Decimal
    max_daily_rebound: Decimal
    reach_days_5: int
    reach_days_10: int
    reach_days_15: int
    current_to_window_high: Decimal
    lower_trend: Decimal
    box_inclusion: Decimal
    hour_bars: list[HourBar]
    hourly_closes: list[Decimal]
    score: Decimal


@dataclass(frozen=True, slots=True)
class FilterAuditEntry:
    rank: int
    symbol: str
    name: str
    score: Decimal
    eligible: bool
    rejection_reasons: tuple[str, ...]
    position_pct: Decimal
    lower_trend_pct: Decimal
    target_reach_count: int


class MinuteBarStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, symbol: str, trading_date: str) -> Path:
        return self.root / symbol / f"{trading_date}.json"

    def load(self, symbol: str, trading_date: str) -> list[KisMinuteBar]:
        path = self.path_for(symbol, trading_date)
        if not path.exists():
            return []
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("symbol") != symbol or body.get("trading_date") != trading_date:
            return []
        return [KisMinuteBar(**item) for item in body.get("bars", [])]

    def is_complete(self, symbol: str, trading_date: str) -> bool:
        bars = self.load(symbol, trading_date)
        if len(bars) < 180:
            return False
        return bars[0].trading_time <= "091000" and bars[-1].trading_time >= "152000"

    def save(self, symbol: str, trading_date: str, bars: list[KisMinuteBar]) -> Path:
        if not bars:
            raise ValueError("minute bars must not be empty")
        path = self.path_for(symbol, trading_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": "kis-minute-bars-v1",
                    "provider": "KIS",
                    "symbol": symbol,
                    "trading_date": trading_date,
                    "fetched_at": datetime.now(KST).isoformat(),
                    "bars": [asdict(item) for item in bars],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path


def balanced_prefilter(dataset: MarketDataset) -> list[PrefilterCandidate]:
    result: list[PrefilterCandidate] = []
    for symbol, bars in dataset.bars.items():
        if len(bars) < 7:
            continue
        name = dataset.names.get(symbol, symbol).strip()
        if EXCLUDED_NAME.search(name):
            continue
        latest = bars[-1]
        market_cap = dataset.market_caps.get(symbol, Decimal("0"))
        average_value = sum(
            (bar.trading_value for bar in bars[-7:]), Decimal("0")
        ) / Decimal("7")
        if (
            market_cap >= MIN_CAPITALIZATION
            and latest.close >= MIN_PRICE
            and average_value >= MIN_AVERAGE_TRADING_VALUE
        ):
            result.append(
                PrefilterCandidate(
                    symbol=symbol,
                    name=name,
                    market_cap=market_cap,
                    latest_price=latest.close,
                    average_trading_value=average_value,
                )
            )
    return sorted(result, key=lambda item: item.average_trading_value, reverse=True)


async def backfill_minute_bars(
    client: KisClient,
    store: MinuteBarStore,
    candidates: list[PrefilterCandidate],
    trading_dates: list[date],
    *,
    window_days: int = 7,
    progress: Callable[[str], None] = print,
) -> None:
    if window_days not in {7, 14, 21}:
        raise ValueError("window_days must be 7, 14, or 21")
    required_dates = [
        item.strftime("%Y%m%d") for item in trading_dates[-window_days:]
    ]
    total = len(candidates) * len(required_dates)
    completed = 0
    for candidate in candidates:
        for trading_date in required_dates:
            if store.is_complete(candidate.symbol, trading_date):
                completed += 1
                continue
            last_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    bars = await client.minute_bars_for_day(
                        candidate.symbol,
                        trading_date=trading_date,
                    )
                    store.save(candidate.symbol, trading_date, bars)
                    last_error = None
                    break
                except KisApiError as exc:
                    last_error = exc
                    await asyncio.sleep(attempt * 2)
            if last_error is not None:
                raise CandidateReportError(
                    f"minute backfill failed for {candidate.symbol} {trading_date}: "
                    f"{last_error}"
                ) from last_error
            completed += 1
            progress(
                f"backfill {completed}/{total} "
                f"{candidate.symbol} {trading_date}"
            )


def _percentile(values: Iterable[Decimal], fraction: Decimal) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        raise CandidateReportError("cannot calculate a percentile without values")
    position = fraction * Decimal(len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - Decimal(lower)
    return ordered[lower] * (Decimal("1") - weight) + ordered[upper] * weight


def aggregate_hour_bars(minute_bars: list[KisMinuteBar]) -> list[HourBar]:
    groups: dict[tuple[str, str], list[KisMinuteBar]] = {}
    for bar in minute_bars:
        if not ("090000" <= bar.trading_time <= "153000"):
            continue
        bucket = bar.trading_time[:2]
        groups.setdefault((bar.trading_date, bucket), []).append(bar)
    result: list[HourBar] = []
    for (trading_date, bucket), bars in sorted(groups.items()):
        ordered = sorted(bars, key=lambda item: item.trading_time)
        result.append(
            HourBar(
                trading_date=trading_date,
                bucket=bucket,
                open=Decimal(ordered[0].open),
                high=Decimal(max(item.high for item in ordered)),
                low=Decimal(min(item.low for item in ordered)),
                close=Decimal(ordered[-1].close),
                volume=sum((Decimal(item.volume) for item in ordered), Decimal("0")),
            )
        )
    return result


def _target_reach_episodes(
    minute_bars: list[KisMinuteBar],
    low: Decimal,
    *,
    target_gain: Decimal = Decimal("0.10"),
    max_holding_trading_days: int = 3,
) -> tuple[int, int, int]:
    """Count lower contacts that reach target within the bounded holding window."""
    if max_holding_trading_days < 1:
        raise ValueError("max_holding_trading_days must be positive")
    target = low * (Decimal("1") + target_gain)
    ordered = sorted(
        minute_bars,
        key=lambda item: (item.trading_date, item.trading_time),
    )
    trading_days = sorted({bar.trading_date for bar in ordered})
    day_index = {trading_day: index for index, trading_day in enumerate(trading_days)}
    armed_at: int | None = None
    contacts = 0
    reaches = 0
    for bar in ordered:
        current_day = day_index[bar.trading_date]
        if armed_at is not None:
            if current_day - armed_at >= max_holding_trading_days:
                armed_at = None
            elif Decimal(bar.high) >= target:
                reaches += 1
                armed_at = None
                continue
        if armed_at is None and Decimal(bar.low) <= low:
            contacts += 1
            armed_at = current_day
    return contacts, reaches, int(armed_at is not None)


def _daily_dynamics(
    minute_bars: list[KisMinuteBar],
    current: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal, int, int, int, Decimal, Decimal]:
    grouped: dict[str, list[KisMinuteBar]] = {}
    for bar in sorted(minute_bars, key=lambda item: (item.trading_date, item.trading_time)):
        grouped.setdefault(bar.trading_date, []).append(bar)
    ranges: list[Decimal] = []
    rebounds: list[Decimal] = []
    daily_lows: list[Decimal] = []
    for bars in grouped.values():
        daily_low = min(Decimal(bar.low) for bar in bars)
        daily_high = max(Decimal(bar.high) for bar in bars)
        low_index = next(
            index for index, bar in enumerate(bars) if Decimal(bar.low) == daily_low
        )
        later = bars[low_index + 1 :]
        rebound_high = (
            max(Decimal(bar.high) for bar in later) if later else daily_low
        )
        ranges.append((daily_high / daily_low - Decimal("1")) * HUNDRED)
        rebounds.append(
            max(Decimal("0"), (rebound_high / daily_low - Decimal("1")) * HUNDRED)
        )
        daily_lows.append(daily_low)
    if not ranges:
        raise CandidateReportError("daily dynamics require minute bars")
    reach_days_5 = sum(value >= Decimal("5") for value in rebounds)
    reach_days_10 = sum(value >= Decimal("10") for value in rebounds)
    reach_days_15 = sum(value >= Decimal("15") for value in rebounds)
    window_high = max(Decimal(bar.high) for bar in minute_bars)
    current_to_high = max(
        Decimal("0"), (window_high / current - Decimal("1")) * HUNDRED
    )
    lower_trend = (
        (daily_lows[-1] / daily_lows[0] - Decimal("1")) * HUNDRED
        if len(daily_lows) > 1
        else Decimal("0")
    )
    return (
        _percentile(ranges, Decimal("0.50")),
        max(ranges),
        _percentile(rebounds, Decimal("0.50")),
        max(rebounds),
        reach_days_5,
        reach_days_10,
        reach_days_15,
        current_to_high,
        lower_trend,
    )


def _flow_for(
    dataset: MarketDataset,
    symbol: str,
    days: int,
    average_value_billion: Decimal,
) -> FlowBreakdown:
    values = dataset.flows.get(days, {}).get(symbol, {})
    retail = values.get("retail", Decimal("0"))
    foreign = values.get("foreign", Decimal("0"))
    institution = values.get("institution", Decimal("0"))
    financial = values.get("financial_investment", Decimal("0"))
    pension = values.get("pension", Decimal("0"))
    total_traded_eok = average_value_billion * Decimal(days) * Decimal("10")
    strength = (
        (foreign + institution) / total_traded_eok * HUNDRED
        if total_traded_eok > 0
        else Decimal("0")
    )
    return FlowBreakdown(
        retail=retail.quantize(Decimal("0.1")),
        foreign=foreign.quantize(Decimal("0.1")),
        institution=institution.quantize(Decimal("0.1")),
        financial_investment=financial.quantize(Decimal("0.1")),
        pension=pension.quantize(Decimal("0.1")),
        strength_pct=strength.quantize(Decimal("0.01")),
    )


def _reference_values(
    dataset: MarketDataset,
    symbol: str,
    days: int,
) -> tuple[Decimal, Decimal, Decimal, FlowBreakdown]:
    bars = dataset.bars[symbol][-days:]
    actual_days = Decimal(len(bars))
    period_return = (
        (bars[-1].close / bars[0].close - Decimal("1")) * HUNDRED
        if len(bars) > 1
        else Decimal("0")
    )
    average_value = (
        sum((bar.trading_value for bar in bars), Decimal("0")) / actual_days
    )
    average_value_billion = average_value / Decimal("1000000000")
    prior = dataset.bars[symbol][-days * 2 : -days]
    current_volume = sum((bar.volume for bar in bars), Decimal("0")) / actual_days
    prior_volume = (
        sum((bar.volume for bar in prior), Decimal("0")) / Decimal(len(prior))
        if prior
        else current_volume
    )
    volume_ratio = current_volume / prior_volume if prior_volume > 0 else Decimal("0")
    return (
        period_return.quantize(Decimal("0.01")),
        average_value_billion.quantize(Decimal("0.1")),
        volume_ratio.quantize(Decimal("0.01")),
        _flow_for(dataset, symbol, days, average_value_billion),
    )


def _intraday_period_return(
    current_price: Decimal,
    hourly_closes: list[Decimal],
) -> Decimal:
    if not hourly_closes or hourly_closes[0] <= 0:
        raise CandidateReportError("intraday period return requires a positive first close")
    return (
        (current_price / hourly_closes[0] - Decimal("1")) * HUNDRED
    ).quantize(Decimal("0.01"))


def _grade(score: Decimal) -> AiGrade:
    if score >= Decimal("75"):
        return "STRONG_RECOMMEND"
    if score >= Decimal("60"):
        return "RECOMMEND"
    if score >= Decimal("45"):
        return "NOT_RECOMMEND"
    return "STRONG_NOT_RECOMMEND"


def _entry_location_factor(position: Decimal) -> Decimal:
    if position <= Decimal("20"):
        return Decimal("1.00")
    if position <= Decimal("35"):
        return Decimal("0.90")
    if position <= Decimal("50"):
        return Decimal("0.60")
    if position <= Decimal("70"):
        return Decimal("0.30")
    return Decimal("0.10")


def _setup_grade(
    score: Decimal,
    position: Decimal,
    lower_trend: Decimal,
    target_reaches: int = 1,
) -> AiGrade:
    if (
        position > Decimal("35")
        or lower_trend <= Decimal("-8")
        or target_reaches < 1
    ):
        return "NOT_RECOMMEND" if score >= Decimal("45") else "STRONG_NOT_RECOMMEND"
    return _grade(score)


def _setup_eligible(item: _Analyzed) -> bool:
    return (
        item.position <= Decimal("35")
        and item.lower_trend > Decimal("-8")
        and item.target_reaches >= 1
    )


def _analyze_symbol(symbol: str, minute_bars: list[KisMinuteBar]) -> _Analyzed:
    hour_bars = aggregate_hour_bars(minute_bars)
    if len(hour_bars) < 20:
        raise CandidateReportError(f"{symbol} has too few 60-minute bars")
    low = _percentile((bar.low for bar in hour_bars), Decimal("0.10"))
    high = _percentile((bar.high for bar in hour_bars), Decimal("0.90"))
    if high <= low:
        raise CandidateReportError(f"{symbol} has no robust intraday box")
    center = (high + low) / Decimal("2")
    amplitude = (high - low) / center * HUNDRED
    current = Decimal(minute_bars[-1].close)
    position = (current - low) / (high - low) * HUNDRED
    included = sum(1 for bar in hour_bars if bar.low >= low and bar.high <= high)
    box_inclusion = Decimal(included) / Decimal(len(hour_bars)) * HUNDRED
    lower_contacts, target_reaches, target_pending = _target_reach_episodes(
        minute_bars, low
    )
    (
        median_daily_range,
        max_daily_range,
        median_daily_rebound,
        max_daily_rebound,
        reach_days_5,
        reach_days_10,
        reach_days_15,
        current_to_window_high,
        lower_trend,
    ) = _daily_dynamics(minute_bars, current)
    return _Analyzed(
        symbol=symbol,
        low=low,
        high=high,
        amplitude=amplitude,
        position=position,
        target_price=low * Decimal("1.10"),
        lower_contacts=lower_contacts,
        target_reaches=target_reaches,
        target_pending=target_pending,
        median_daily_range=median_daily_range,
        max_daily_range=max_daily_range,
        median_daily_rebound=median_daily_rebound,
        max_daily_rebound=max_daily_rebound,
        reach_days_5=reach_days_5,
        reach_days_10=reach_days_10,
        reach_days_15=reach_days_15,
        current_to_window_high=current_to_window_high,
        lower_trend=lower_trend,
        box_inclusion=box_inclusion,
        hour_bars=hour_bars,
        hourly_closes=[bar.close for bar in hour_bars],
        score=Decimal("0"),
    )


def _score_all(
    analyses: list[_Analyzed],
    candidates: dict[str, PrefilterCandidate],
) -> list[_Analyzed]:
    max_liquidity_log = max(
        math.log10(float(item.average_trading_value))
        for item in candidates.values()
    )
    result: list[_Analyzed] = []
    for item in analyses:
        day_count = Decimal("7")
        target_frequency_score = min(
            HUNDRED,
            Decimal(item.reach_days_5) / day_count * Decimal("20")
            + Decimal(item.reach_days_10) / day_count * Decimal("30")
            + Decimal(item.reach_days_15) / day_count * Decimal("50"),
        )
        rebound_score = min(
            HUNDRED,
            min(HUNDRED, item.median_daily_rebound / Decimal("10") * HUNDRED)
            * Decimal("0.60")
            + min(HUNDRED, item.max_daily_rebound / Decimal("15") * HUNDRED)
            * Decimal("0.40"),
        )
        daily_range_score = min(
            HUNDRED,
            min(HUNDRED, item.median_daily_range / Decimal("8") * HUNDRED)
            * Decimal("0.60")
            + min(HUNDRED, item.max_daily_range / Decimal("15") * HUNDRED)
            * Decimal("0.40"),
        )
        liquidity_log = Decimal(
            str(math.log10(float(candidates[item.symbol].average_trading_value)))
        )
        liquidity_score = liquidity_log / Decimal(str(max_liquidity_log)) * HUNDRED
        lower_score = max(Decimal("0"), HUNDRED - max(Decimal("0"), item.position))
        upside_room_score = min(
            HUNDRED, item.current_to_window_high / Decimal("15") * HUNDRED
        )
        trend_penalty = min(
            Decimal("15"), max(Decimal("0"), -item.lower_trend) * Decimal("1.5")
        )
        raw_score = (
            upside_room_score * Decimal("0.25")
            + lower_score * Decimal("0.20")
            + target_frequency_score * Decimal("0.20")
            + liquidity_score * Decimal("0.15")
            + rebound_score * Decimal("0.12")
            + daily_range_score * Decimal("0.08")
        )
        score = raw_score * _entry_location_factor(item.position) - trend_penalty
        result.append(
            replace(item, score=min(HUNDRED, max(Decimal("0"), score)))
        )
    return sorted(
        result,
        key=lambda item: (_setup_eligible(item), item.score),
        reverse=True,
    )


def screening_pool(
    candidates: list[PrefilterCandidate],
    store: MinuteBarStore,
    trading_dates: list[date],
    *,
    limit: int = 50,
) -> list[PrefilterCandidate]:
    if limit < 1:
        raise ValueError("screening pool limit must be positive")
    required_dates = [
        item.strftime("%Y%m%d") for item in trading_dates[-7:]
    ]
    analyses: list[_Analyzed] = []
    for candidate in candidates:
        bars: list[KisMinuteBar] = []
        for trading_date in required_dates:
            if not store.is_complete(candidate.symbol, trading_date):
                bars = []
                break
            bars.extend(store.load(candidate.symbol, trading_date))
        if bars:
            analyses.append(_analyze_symbol(candidate.symbol, bars))
    if not analyses:
        raise CandidateReportError("screening pool requires complete 7-day data")
    candidate_map = {item.symbol: item for item in candidates}
    ranked = _score_all(analyses, candidate_map)[:limit]
    return [candidate_map[item.symbol] for item in ranked]


def screening_pool_audit(
    candidates: list[PrefilterCandidate],
    store: MinuteBarStore,
    trading_dates: list[date],
    *,
    limit: int = 50,
) -> list[FilterAuditEntry]:
    pool = screening_pool(candidates, store, trading_dates, limit=limit)
    required_dates = [item.strftime("%Y%m%d") for item in trading_dates[-7:]]
    candidate_map = {item.symbol: item for item in candidates}
    analyses: list[_Analyzed] = []
    for candidate in pool:
        bars = [
            bar
            for trading_date in required_dates
            for bar in store.load(candidate.symbol, trading_date)
        ]
        analyses.append(_analyze_symbol(candidate.symbol, bars))
    ranked = _score_all(analyses, candidate_map)
    entries: list[FilterAuditEntry] = []
    for rank, item in enumerate(ranked, start=1):
        reasons: list[str] = []
        if item.position > Decimal("35"):
            reasons.append("CURRENT_POSITION_ABOVE_35")
        if item.lower_trend <= Decimal("-8"):
            reasons.append("LOWER_TREND_AT_OR_BELOW_MINUS_8")
        if item.target_reaches < 1:
            reasons.append("NO_LOWER_CONTACT_TO_PLUS_10_WITHIN_3_DAYS")
        entries.append(
            FilterAuditEntry(
                rank=rank,
                symbol=item.symbol,
                name=candidate_map[item.symbol].name,
                score=item.score.quantize(Decimal("0.01")),
                eligible=not reasons,
                rejection_reasons=tuple(reasons),
                position_pct=item.position.quantize(Decimal("0.01")),
                lower_trend_pct=item.lower_trend.quantize(Decimal("0.01")),
                target_reach_count=item.target_reaches,
            )
        )
    return entries


def _window_analysis(
    store: MinuteBarStore,
    symbol: str,
    trading_dates: list[date],
    days: int,
) -> tuple[_Analyzed | None, int]:
    required_dates = [
        item.strftime("%Y%m%d") for item in trading_dates[-days:]
    ]
    completed = sum(
        store.is_complete(symbol, trading_date)
        for trading_date in required_dates
    )
    if completed != days:
        return None, completed
    bars: list[KisMinuteBar] = []
    for trading_date in required_dates:
        bars.extend(store.load(symbol, trading_date))
    ordered = sorted(
        bars,
        key=lambda item: (item.trading_date, item.trading_time),
    )
    return _analyze_symbol(symbol, ordered), completed


def _ready_window_metrics(
    dataset: MarketDataset,
    analysis: _Analyzed,
    *,
    days: WindowDays,
    rank: int,
) -> WindowMetrics:
    symbol = analysis.symbol
    current_price = analysis.hourly_closes[-1]
    current_vs_high = min(
        Decimal("0"),
        (current_price / analysis.high - Decimal("1")) * HUNDRED,
    )
    _, average_value, volume_ratio, flows = _reference_values(
        dataset,
        symbol,
        days,
    )
    period_return = _intraday_period_return(
        current_price,
        analysis.hourly_closes,
    )
    risk = min(
        HUNDRED,
        max(Decimal("0"), -period_return) * Decimal("2")
        + (Decimal("20") if analysis.reach_days_5 == 0 else Decimal("0"))
        + max(Decimal("0"), Decimal("70") - analysis.box_inclusion),
    )
    score = analysis.score.quantize(Decimal("0.01"))
    grade = _setup_grade(
        score,
        analysis.position,
        analysis.lower_trend,
        analysis.target_reaches,
    )
    return WindowMetrics(
        days=days,
        structure_status="READY",
        structure_completed_days=days,
        rank=rank,
        box_low=analysis.low.quantize(Decimal("0.01")),
        box_high=analysis.high.quantize(Decimal("0.01")),
        amplitude_pct=analysis.amplitude.quantize(Decimal("0.01")),
        position_pct=analysis.position.quantize(Decimal("0.01")),
        median_daily_range_pct=analysis.median_daily_range.quantize(
            Decimal("0.01")
        ),
        max_daily_range_pct=analysis.max_daily_range.quantize(Decimal("0.01")),
        median_daily_rebound_pct=analysis.median_daily_rebound.quantize(
            Decimal("0.01")
        ),
        max_daily_rebound_pct=analysis.max_daily_rebound.quantize(
            Decimal("0.01")
        ),
        reach_days_5pct=analysis.reach_days_5,
        reach_days_10pct=analysis.reach_days_10,
        reach_days_15pct=analysis.reach_days_15,
        current_vs_window_high_pct=current_vs_high.quantize(Decimal("0.01")),
        lower_trend_pct=analysis.lower_trend.quantize(Decimal("0.01")),
        lower_trend=(
            "상승"
            if analysis.lower_trend > Decimal("2")
            else "하락"
            if analysis.lower_trend < Decimal("-2")
            else "횡보"
        ),
        return_pct=period_return,
        average_trading_value_billion=average_value,
        volume_ratio=volume_ratio,
        target_price_10pct=analysis.target_price.quantize(Decimal("0.01")),
        lower_contact_count=analysis.lower_contacts,
        target_reach_count=analysis.target_reaches,
        target_pending_count=analysis.target_pending,
        breakdown_risk_pct=risk.quantize(Decimal("0.01")),
        quant_score=score,
        ai_score=score,
        final_score=score,
        ai_grade=grade,
        ai_comment=(
            f"실제 {days}거래일 1분봉을 60분봉으로 집계한 정량 기준선입니다. "
            f"일중 진폭 중앙값 "
            f"{analysis.median_daily_range.quantize(Decimal('0.1'))}%, "
            f"저점 반등 중앙값 "
            f"{analysis.median_daily_rebound.quantize(Decimal('0.1'))}%, "
            f"+5/+10/+15% 도달 {analysis.reach_days_5}/"
            f"{analysis.reach_days_10}/{analysis.reach_days_15}일, "
            f"하단 접촉 후 3거래일 이내 +10% "
            f"{analysis.target_reaches}회입니다. "
            "뉴스·공시를 포함한 AI 전수 검토는 아직 적용 전입니다."
        ),
        reasons=[
            f"일중 진폭 중앙값 "
            f"{analysis.median_daily_range.quantize(Decimal('0.1'))}%",
            f"저점 반등 중앙값 "
            f"{analysis.median_daily_rebound.quantize(Decimal('0.1'))}%",
            f"3거래일 이내 하단→+10% {analysis.target_reaches}회",
        ],
        risks=["연구용 기준선이며 뉴스·공시·호가 검증 전"],
        invalidation=(
            f"박스 하단 {analysis.low.quantize(Decimal('1'))}원 "
            "이탈 후 재진입 실패"
        ),
        closes=analysis.hourly_closes,
        chart_bars=[
            ChartBar(
                trading_date=bar.trading_date,
                bucket=bar.bucket,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            for bar in analysis.hour_bars
        ],
        flows=flows,
    )


def build_intraday_report(
    dataset: MarketDataset,
    candidates: list[PrefilterCandidate],
    store: MinuteBarStore,
) -> DashboardReport:
    base_analyses: list[_Analyzed] = []
    for candidate in candidates:
        analysis, _ = _window_analysis(
            store,
            candidate.symbol,
            dataset.trading_dates,
            7,
        )
        if analysis is not None:
            base_analyses.append(analysis)
    if len(base_analyses) < 30:
        raise CandidateReportError(
            f"only {len(base_analyses)} balanced symbols have complete "
            "7-day minute data"
        )
    candidate_map = {item.symbol: item for item in candidates}
    selected = [
        item
        for item in _score_all(base_analyses, candidate_map)
        if _setup_eligible(item)
    ][:30]
    if not selected:
        raise CandidateReportError("no symbols passed the +10% qualification gate")
    selected_symbols = [item.symbol for item in selected]
    analyses_by_window: dict[int, dict[str, _Analyzed]] = {}
    ranks_by_window: dict[int, dict[str, int]] = {}
    completed_by_window: dict[int, dict[str, int]] = {}
    for days in WINDOW_DAYS:
        window_analyses: list[_Analyzed] = []
        completed_by_window[days] = {}
        for symbol in selected_symbols:
            analysis, completed = _window_analysis(
                store,
                symbol,
                dataset.trading_dates,
                days,
            )
            completed_by_window[days][symbol] = completed
            if analysis is not None:
                window_analyses.append(analysis)
        if len(window_analyses) == len(selected_symbols):
            ranked = _score_all(window_analyses, candidate_map)
            analyses_by_window[days] = {
                item.symbol: item for item in ranked
            }
            ranks_by_window[days] = {
                item.symbol: rank
                for rank, item in enumerate(ranked, start=1)
            }
    views: list[CandidateView] = []
    for base_analysis in selected:
        symbol = base_analysis.symbol
        windows: dict[str, WindowMetrics] = {}
        for days in WINDOW_DAYS:
            analysis = analyses_by_window.get(days, {}).get(symbol)
            if analysis is not None:
                windows[str(days)] = _ready_window_metrics(
                    dataset,
                    analysis,
                    days=days,
                    rank=ranks_by_window[days][symbol],
                )
                continue
            return_pct, avg_value, ratio, window_flows = _reference_values(
                dataset,
                symbol,
                days,
            )
            windows[str(days)] = WindowMetrics(
                days=days,
                structure_status="WARMING_UP",
                structure_completed_days=completed_by_window[days][symbol],
                return_pct=return_pct,
                average_trading_value_billion=avg_value,
                volume_ratio=ratio,
                flows=window_flows,
            )
        current_price = analyses_by_window[7][symbol].hourly_closes[-1]
        name = candidate_map[symbol].name
        views.append(
            CandidateView(
                code=symbol,
                name=name,
                sector="KOSPI",
                current_price=current_price,
                windows=windows,  # type: ignore[arg-type]
                news=[],
                discussion_summary="실제 분봉 기준 연구용 후보이며 AI 뉴스 검토 전입니다.",
                naver_url=HttpUrl(
                    f"https://finance.naver.com/item/main.naver?code={symbol}"
                ),
            )
        )
    data_date = dataset.trading_dates[-1]
    ready_windows = [
        str(days)
        for days in WINDOW_DAYS
        if days in analyses_by_window
    ]
    return DashboardReport(
        generated_at=datetime.now(KST),
        data_as_of=datetime.combine(data_date, time(15, 30), tzinfo=KST),
        market_regime=(
            f"실제 {'·'.join(ready_windows)}거래일 분봉 · "
            "+10% 자격 게이트 연구 기준선"
        ),
        calculation_version=(
            "intraday-elasticity-v5-target-reach-qualified-chart-aligned-"
            "prefilter-balanced-v1"
        ),
        strategy_status="RESEARCH_ONLY",
        source_bar_interval_minutes=1,
        analysis_bar_interval_minutes=60,
        model_id="quant-intraday-baseline-no-llm",
        prompt_version="not-applied",
        is_demo=False,
        candidates=views,
        extended_watchlist=[],
    )
