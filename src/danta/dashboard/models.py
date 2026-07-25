from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

WindowKey = Literal["7", "14", "21"]
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


class WindowMetrics(BaseModel):
    days: Literal[7, 14, 21]
    structure_status: Literal["READY", "WARMING_UP"] = "READY"
    structure_completed_days: int | None = Field(default=None, ge=0, le=21)
    rank: int | None = Field(default=None, ge=1, le=30)
    box_low: Decimal | None = Field(default=None, gt=0)
    box_high: Decimal | None = Field(default=None, gt=0)
    amplitude_pct: Decimal | None = Field(default=None, ge=0)
    position_pct: Decimal | None = None
    return_pct: Decimal
    average_trading_value_billion: Decimal = Field(ge=0)
    volume_ratio: Decimal = Field(ge=0)
    traversal_count: int | None = Field(default=None, ge=0)
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
            self.traversal_count,
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
            if len(self.closes) < 2:
                raise ValueError("READY structure must include chart closes")
        elif any(value is not None for value in structural) or self.closes:
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
    candidates: list[CandidateView] = Field(min_length=30, max_length=30)

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
        codes = [candidate.code for candidate in self.candidates]
        if len(set(codes)) != 30:
            raise ValueError("candidate codes must be unique")
        windows: tuple[WindowKey, WindowKey, WindowKey] = ("7", "14", "21")
        for window in windows:
            metrics = [candidate.windows[window] for candidate in self.candidates]
            ready = [item for item in metrics if item.structure_status == "READY"]
            if ready and len(ready) != 30:
                raise ValueError(f"candidate structures for {window} days must share one status")
            ranks = [item.rank for item in ready if item.rank is not None]
            if ready and sorted(ranks) != list(range(1, 31)):
                raise ValueError(f"candidate ranks for {window} days must be 1 through 30")
        return self
