from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

WindowKey = Literal["7", "14", "21"]
CANDIDATE_COUNT = 30
EXTENDED_WATCHLIST_COUNT = 20
Sentiment = Literal["POSITIVE", "NEUTRAL", "NEGATIVE"]
AiGrade = Literal[
    "STRONG_RECOMMEND",
    "RECOMMEND",
    "NOT_RECOMMEND",
    "STRONG_NOT_RECOMMEND",
]


class FlowBreakdown(BaseModel):
    retail: Decimal
    foreign: Decimal
    institution: Decimal
    financial_investment: Decimal
    pension: Decimal
    strength_pct: Decimal


class ChartBar(BaseModel):
    trading_date: str = Field(pattern=r"^\d{8}$")
    bucket: str = Field(pattern=r"^\d{2}$")
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_ohlc(self) -> ChartBar:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be the greatest OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be the smallest OHLC value")
        return self


class WindowMetrics(BaseModel):
    days: Literal[7, 14, 21]
    structure_status: Literal["READY", "WARMING_UP"] = "READY"
    structure_completed_days: int | None = Field(default=None, ge=0, le=21)
    rank: int | None = Field(
        default=None,
        ge=1,
        le=CANDIDATE_COUNT + EXTENDED_WATCHLIST_COUNT,
    )
    box_low: Decimal | None = Field(default=None, gt=0)
    box_high: Decimal | None = Field(default=None, gt=0)
    amplitude_pct: Decimal | None = Field(default=None, ge=0)
    position_pct: Decimal | None = None
    median_daily_range_pct: Decimal | None = Field(default=None, ge=0)
    max_daily_range_pct: Decimal | None = Field(default=None, ge=0)
    median_daily_rebound_pct: Decimal | None = Field(default=None, ge=0)
    max_daily_rebound_pct: Decimal | None = Field(default=None, ge=0)
    reach_days_5pct: int | None = Field(default=None, ge=0, le=21)
    reach_days_10pct: int | None = Field(default=None, ge=0, le=21)
    reach_days_15pct: int | None = Field(default=None, ge=0, le=21)
    current_vs_window_high_pct: Decimal | None = Field(default=None, le=0)
    lower_trend_pct: Decimal | None = None
    lower_trend: Literal["상승", "횡보", "하락"] | None = None
    return_pct: Decimal
    average_trading_value_billion: Decimal = Field(ge=0)
    volume_ratio: Decimal = Field(ge=0)
    target_price_10pct: Decimal | None = Field(default=None, gt=0)
    lower_contact_count: int | None = Field(default=None, ge=0)
    target_reach_count: int | None = Field(default=None, ge=0)
    target_pending_count: int | None = Field(default=None, ge=0, le=1)
    target_expired_count: int | None = Field(default=None, ge=0)
    breakdown_risk_pct: Decimal | None = Field(default=None, ge=0, le=100)
    quant_score: Decimal | None = Field(default=None, ge=0, le=100)
    ai_score: Decimal | None = Field(default=None, ge=0, le=100)
    final_score: Decimal | None = Field(default=None, ge=0, le=100)
    ai_grade: AiGrade | None = None
    ai_comment: str | None = Field(default=None, min_length=1, max_length=400)
    reasons: list[str] = Field(default_factory=list, max_length=5)
    risks: list[str] = Field(default_factory=list, max_length=5)
    invalidation: str | None = Field(default=None, min_length=1, max_length=240)
    closes: list[Decimal] = Field(default_factory=list, max_length=200)
    chart_bars: list[ChartBar] = Field(default_factory=list, max_length=200)
    flows: FlowBreakdown

    @model_validator(mode="after")
    def validate_box(self) -> WindowMetrics:
        if self.structure_completed_days is None:
            self.structure_completed_days = self.days
        if self.structure_completed_days > self.days:
            raise ValueError("structure_completed_days must not exceed days")
        if self.structure_status == "READY" and self.structure_completed_days != self.days:
            raise ValueError("READY structure must have complete trading days")
        if self.structure_status == "WARMING_UP" and self.structure_completed_days >= self.days:
            raise ValueError("WARMING_UP structure must be incomplete")
        structural = (
            self.rank,
            self.box_low,
            self.box_high,
            self.amplitude_pct,
            self.position_pct,
            self.median_daily_range_pct,
            self.max_daily_range_pct,
            self.median_daily_rebound_pct,
            self.max_daily_rebound_pct,
            self.reach_days_5pct,
            self.reach_days_10pct,
            self.reach_days_15pct,
            self.current_vs_window_high_pct,
            self.lower_trend_pct,
            self.lower_trend,
            self.target_price_10pct,
            self.lower_contact_count,
            self.target_reach_count,
            self.target_pending_count,
            self.target_expired_count,
            self.breakdown_risk_pct,
            self.quant_score,
            self.ai_score,
            self.final_score,
            self.ai_grade,
            self.ai_comment,
            self.invalidation,
        )
        if self.structure_status == "READY":
            if any(value is None for value in structural):
                raise ValueError("READY structure must include all structure and review fields")
            if not self.reasons or not self.risks:
                raise ValueError("READY structure must include reasons and risks")
            if self.box_high is None or self.box_low is None or self.box_high <= self.box_low:
                raise ValueError("box_high must be greater than box_low")
            if (
                self.lower_contact_count is None
                or self.target_reach_count is None
                or self.target_pending_count is None
                or self.target_expired_count is None
                or self.lower_contact_count
                != (
                    self.target_reach_count
                    + self.target_pending_count
                    + self.target_expired_count
                )
            ):
                raise ValueError(
                    "lower contacts must equal reached plus pending plus expired episodes"
                )
            if len(self.closes) < 2:
                raise ValueError("READY structure must include chart closes")
            if len(self.chart_bars) != len(self.closes):
                raise ValueError("READY structure must include one OHLC bar per chart close")
        elif any(value is not None for value in structural) or self.closes or self.chart_bars:
            raise ValueError("WARMING_UP structure must not contain fabricated structure fields")
        return self


class NewsItem(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    source: str = Field(min_length=1, max_length=80)
    published_at: datetime
    url: HttpUrl
    sentiment: Sentiment = "NEUTRAL"


class CandidateView(BaseModel):
    code: str = Field(pattern=r"^[0-9A-Z]{6}$")
    name: str = Field(min_length=1, max_length=80)
    sector: str = Field(min_length=1, max_length=80)
    current_price: Decimal = Field(gt=0)
    windows: dict[WindowKey, WindowMetrics]
    news: list[NewsItem] = Field(max_length=5)
    discussion_summary: str = Field(max_length=500)
    naver_url: HttpUrl

    @model_validator(mode="after")
    def validate_windows_and_selection(self) -> CandidateView:
        if set(self.windows) != {"7", "14", "21"}:
            raise ValueError("windows must contain exactly 7, 14, and 21")
        expected_days: dict[WindowKey, int] = {"7": 7, "14": 14, "21": 21}
        for key, days in expected_days.items():
            if self.windows[key].days != days:
                raise ValueError(f"window {key} has mismatched days")
        return self


class DashboardReport(BaseModel):
    report_title: str = "Danta 단기 변동성 후보 리포트"
    generated_at: datetime
    data_as_of: datetime
    market: str = "KOSPI"
    market_regime: str
    calculation_version: str
    strategy_status: Literal["RESEARCH_ONLY", "ACTIVE"] = "RESEARCH_ONLY"
    source_bar_interval_minutes: int | None = Field(default=None, gt=0)
    analysis_bar_interval_minutes: int | None = Field(default=None, gt=0)
    model_id: str
    prompt_version: str
    is_demo: bool = False
    candidates: list[CandidateView] = Field(min_length=1, max_length=30)
    extended_watchlist: list[CandidateView] = Field(
        default_factory=list,
        max_length=EXTENDED_WATCHLIST_COUNT,
    )

    @model_validator(mode="after")
    def validate_candidate_set(self) -> DashboardReport:
        if self.strategy_status == "ACTIVE" and (
            self.source_bar_interval_minutes != 1
            or self.analysis_bar_interval_minutes not in {10, 30, 60}
        ):
            raise ValueError(
                "ACTIVE reports require 1-minute source and an approved "
                "10/30/60-minute analysis bar"
            )
        codes = [
            candidate.code
            for candidate in [*self.candidates, *self.extended_watchlist]
        ]
        if len(set(codes)) != len(codes):
            raise ValueError("candidate codes must be unique")
        windows: tuple[WindowKey, WindowKey, WindowKey] = ("7", "14", "21")
        for window in windows:
            metrics = [candidate.windows[window] for candidate in self.candidates]
            ready = [item for item in metrics if item.structure_status == "READY"]
            if ready and len(ready) != len(metrics):
                raise ValueError(f"candidate structures for {window} days must share one status")
            ranks = [item.rank for item in ready if item.rank is not None]
            if ready and sorted(ranks) != list(range(1, len(ready) + 1)):
                raise ValueError(
                    f"candidate ranks for {window} days must be contiguous"
                )
            extended = [
                candidate.windows[window] for candidate in self.extended_watchlist
            ]
            extended_ready = [
                item for item in extended if item.structure_status == "READY"
            ]
            if extended_ready and len(extended_ready) != len(extended):
                raise ValueError(
                    f"extended watchlist structures for {window} days "
                    "must share one status"
                )
            extended_ranks = [
                item.rank for item in extended_ready if item.rank is not None
            ]
            extended_start = len(self.candidates) + 1
            if extended_ready and sorted(extended_ranks) != list(
                range(extended_start, extended_start + len(extended_ready))
            ):
                raise ValueError(
                    "extended watchlist ranks must follow official candidates"
                )
        return self
