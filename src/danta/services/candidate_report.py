from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from statistics import median
from typing import Literal

from pydantic import HttpUrl

from danta.adapters.krx.client import DailyBar, MarketDataset
from danta.dashboard.models import (
    AiGrade,
    CandidateView,
    ChartBar,
    DashboardReport,
    FlowBreakdown,
    NewsItem,
    WindowMetrics,
)

KST = timezone(timedelta(hours=9))
WINDOWS: tuple[Literal[7], Literal[14], Literal[21]] = (7, 14, 21)
ONE = Decimal("1")
HUNDRED = Decimal("100")


class CandidateReportError(RuntimeError):
    """Raised when a valid set of 30 KOSPI candidates cannot be produced."""


def _round(value: Decimal, places: str = "0.01") -> Decimal:
    return value.quantize(Decimal(places))


def _target_reach_episodes(
    closes: list[Decimal],
    low: Decimal,
) -> tuple[int, int, int]:
    target = low * Decimal("1.10")
    armed = False
    contacts = 0
    reaches = 0
    for close in closes:
        if armed:
            if close >= target:
                reaches += 1
                armed = False
            continue
        if close <= low:
            contacts += 1
            armed = True
    return contacts, reaches, int(armed)


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
        retail=_round(retail, "0.1"),
        foreign=_round(foreign, "0.1"),
        institution=_round(institution, "0.1"),
        financial_investment=_round(financial, "0.1"),
        pension=_round(pension, "0.1"),
        strength_pct=_round(strength),
    )


def _grade(score: Decimal) -> AiGrade:
    if score >= 75:
        return "STRONG_RECOMMEND"
    if score >= 60:
        return "RECOMMEND"
    if score >= 45:
        return "NOT_RECOMMEND"
    return "STRONG_NOT_RECOMMEND"


def _ready_score(metrics: WindowMetrics) -> Decimal:
    if metrics.quant_score is None:
        raise CandidateReportError("READY metrics are missing quant_score")
    return metrics.quant_score


def _ready_rank(metrics: WindowMetrics) -> int:
    if metrics.rank is None:
        raise CandidateReportError("READY metrics are missing rank")
    return metrics.rank


def _metrics(
    dataset: MarketDataset,
    symbol: str,
    bars: list[DailyBar],
    days: Literal[7, 14, 21],
    *,
    rank: int,
) -> WindowMetrics:
    selected = bars[-days:]
    closes = [bar.close for bar in selected]
    low, high = min(closes), max(closes)
    if high <= low:
        raise CandidateReportError(f"{symbol} has no price range in the {days}-day window")
    midpoint = (high + low) / Decimal("2")
    amplitude = (high - low) / midpoint * HUNDRED
    position = (closes[-1] - low) / (high - low) * HUNDRED
    period_return = (closes[-1] / closes[0] - ONE) * HUNDRED
    average_value = sum((bar.trading_value for bar in selected), Decimal("0")) / Decimal(
        days
    )
    average_value_billion = average_value / Decimal("1000000000")
    prior = bars[-days * 2 : -days]
    current_average_volume = sum((bar.volume for bar in selected), Decimal("0")) / Decimal(
        days
    )
    prior_average_volume = (
        sum((bar.volume for bar in prior), Decimal("0")) / Decimal(len(prior))
        if prior
        else current_average_volume
    )
    volume_ratio = (
        current_average_volume / prior_average_volume
        if prior_average_volume > 0
        else Decimal("0")
    )
    lower_contacts, target_reaches, target_pending = _target_reach_episodes(
        closes, low
    )
    moves = [
        abs((closes[index] / closes[index - 1] - ONE) * HUNDRED)
        for index in range(1, len(closes))
    ] or [Decimal("0")]
    median_move = Decimal(median(moves))
    max_move = max(moves)
    current_to_high = max(Decimal("0"), (high / closes[-1] - ONE) * HUNDRED)
    lower_trend = (closes[-1] / closes[0] - ONE) * HUNDRED
    downside_trend = max(Decimal("0"), -period_return)
    risk = min(
        HUNDRED,
        downside_trend * Decimal("3")
        + (Decimal("20") if target_reaches == 0 else Decimal("0"))
        + (Decimal("20") if average_value_billion < 10 else Decimal("0")),
    )
    amplitude_score = min(HUNDRED, amplitude / Decimal("15") * HUNDRED)
    target_reach_score = min(
        HUNDRED, Decimal(target_reaches) / Decimal("2") * HUNDRED
    )
    liquidity_score = min(HUNDRED, average_value_billion / Decimal("100") * HUNDRED)
    lower_score = max(Decimal("0"), HUNDRED - position)
    stability_score = HUNDRED - risk
    quant_score = (
        target_reach_score * Decimal("0.35")
        + amplitude_score * Decimal("0.20")
        + liquidity_score * Decimal("0.20")
        + lower_score * Decimal("0.15")
        + stability_score * Decimal("0.10")
    )
    grade = _grade(quant_score)
    flows = _flow_for(dataset, symbol, days, average_value_billion)
    reasons = [
        f"{days}일 진폭 {_round(amplitude)}%",
        f"하단+10% 목표 도달 {target_reaches}회",
        f"일평균 거래대금 {_round(average_value_billion, '0.1')}십억원",
    ]
    risks = [
        (
            "하방 추세와 박스 하단 이탈 여부 확인 필요"
            if period_return < 0
            else "상단 접근 시 추격매수 위험"
        )
    ]
    return WindowMetrics(
        days=days,
        rank=rank,
        box_low=low,
        box_high=high,
        amplitude_pct=_round(amplitude),
        position_pct=_round(position),
        median_daily_range_pct=_round(median_move),
        max_daily_range_pct=_round(max_move),
        median_daily_rebound_pct=_round(median_move),
        max_daily_rebound_pct=_round(max_move),
        reach_days_5pct=sum(value >= 5 for value in moves),
        reach_days_10pct=sum(value >= 10 for value in moves),
        reach_days_15pct=sum(value >= 15 for value in moves),
        current_to_window_high_pct=_round(current_to_high),
        lower_trend_pct=_round(lower_trend),
        lower_trend=(
            "상승"
            if lower_trend > 2
            else "하락"
            if lower_trend < -2
            else "횡보"
        ),
        return_pct=_round(period_return),
        average_trading_value_billion=_round(average_value_billion, "0.1"),
        volume_ratio=_round(volume_ratio),
        target_price_10pct=_round(low * Decimal("1.10")),
        lower_contact_count=lower_contacts,
        target_reach_count=target_reaches,
        target_pending_count=target_pending,
        breakdown_risk_pct=_round(risk),
        quant_score=_round(quant_score),
        ai_score=_round(quant_score),
        final_score=_round(quant_score),
        ai_grade=grade,
        ai_comment=(
            f"정량 기준선: {days}일 진폭 {_round(amplitude)}%, 하단+10% 목표 도달 "
            f"{target_reaches}회, 박스 위치 {_round(position)}%입니다. "
            "뉴스·공시·AI 전수 검토는 아직 적용되지 않았습니다."
        ),
        reasons=reasons,
        risks=risks,
        invalidation=f"{days}일 하단 {_round(low, '1')}원 이탈 후 재진입 실패",
        closes=closes,
        chart_bars=[
            ChartBar(
                trading_date=bar.trading_date.strftime("%Y%m%d"),
                bucket="15",
                open=bar.close,
                high=bar.close,
                low=bar.close,
                close=bar.close,
                volume=bar.volume,
            )
            for bar in selected
        ],
        flows=flows,
    )


def build_quant_report(dataset: MarketDataset) -> DashboardReport:
    provisional: list[tuple[str, WindowMetrics]] = []
    for symbol, bars in dataset.bars.items():
        if len(bars) < 21:
            continue
        try:
            metrics = _metrics(dataset, symbol, bars, 14, rank=1)
        except CandidateReportError:
            continue
        if (
            bars[-1].close < Decimal("1000")
            or metrics.average_trading_value_billion < Decimal("5")
            or (metrics.amplitude_pct or Decimal("0")) < Decimal("3")
        ):
            continue
        provisional.append((symbol, metrics))
    provisional.sort(key=lambda item: _ready_score(item[1]), reverse=True)
    selected_symbols = [symbol for symbol, _ in provisional[:30]]
    if len(selected_symbols) != 30:
        raise CandidateReportError(
            f"only {len(selected_symbols)} candidates passed the data and liquidity gates"
        )

    metrics_by_window: dict[int, dict[str, WindowMetrics]] = {}
    for days in WINDOWS:
        window_values = [
            (symbol, _metrics(dataset, symbol, dataset.bars[symbol], days, rank=1))
            for symbol in selected_symbols
        ]
        window_values.sort(key=lambda item: _ready_score(item[1]), reverse=True)
        ranked: dict[str, WindowMetrics] = {}
        for rank, (symbol, metrics) in enumerate(window_values, start=1):
            ranked[symbol] = metrics.model_copy(update={"rank": rank})
        metrics_by_window[days] = ranked

    candidates: list[CandidateView] = []
    selected_symbols.sort(key=lambda symbol: _ready_rank(metrics_by_window[14][symbol]))
    data_date = dataset.trading_dates[-1]
    published_at = datetime.combine(data_date, time(15, 30), tzinfo=KST)
    for symbol in selected_symbols:
        name = dataset.names.get(symbol, symbol)
        naver_url = HttpUrl(f"https://finance.naver.com/item/main.naver?code={symbol}")
        candidates.append(
            CandidateView(
                code=symbol,
                name=name,
                sector="KOSPI",
                current_price=dataset.bars[symbol][-1].close,
                windows={
                    "7": metrics_by_window[7][symbol],
                    "14": metrics_by_window[14][symbol],
                    "21": metrics_by_window[21][symbol],
                },
                news=[
                    NewsItem(
                        title="최신 뉴스 확인 — 자동 뉴스 수집기 연결 전",
                        source="네이버 증권",
                        published_at=published_at,
                        url=naver_url,
                        sentiment="NEUTRAL",
                    )
                ],
                discussion_summary=(
                    "실제 KRX 가격·수급 기반 정량 보고서입니다. "
                    "뉴스·토론·AI 정성 검토는 아직 연결되지 않았습니다."
                ),
                naver_url=naver_url,
            )
        )
    average_return = sum(
        (candidate.windows["14"].return_pct for candidate in candidates),
        Decimal("0"),
    ) / Decimal(len(candidates))
    regime = "상승" if average_return > 3 else "하락" if average_return < -3 else "중립"
    now = datetime.now(KST).replace(microsecond=0)
    return DashboardReport(
        generated_at=now,
        data_as_of=published_at,
        market_regime=f"KRX · {regime}",
        calculation_version="box-quant-v1",
        model_id="quant-baseline-no-llm",
        prompt_version="candidate-review-not-connected",
        is_demo=False,
        candidates=candidates,
    )
