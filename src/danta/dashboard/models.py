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
    rank: int = Field(ge=1, le=30)
    box_low: Decimal = Field(gt=0)
    box_high: Decimal = Field(gt=0)
    amplitude_pct: Decimal = Field(ge=0)
    position_pct: Decimal
    return_pct: Decimal
    average_trading_value_billion: Decimal = Field(ge=0)
    volume_ratio: Decimal = Field(ge=0)
    traversal_count: int = Field(ge=0)
    breakdown_risk_pct: Decimal = Field(ge=0, le=100)
    quant_score: Decimal = Field(ge=0, le=100)
    ai_score: Decimal = Field(ge=0, le=100)
    final_score: Decimal = Field(ge=0, le=100)
    ai_grade: AiGrade
    ai_comment: str = Field(min_length=1, max_length=400)
    reasons: list[str] = Field(min_length=1, max_length=5)
    risks: list[str] = Field(min_length=1, max_length=5)
    invalidation: str = Field(min_length=1, max_length=240)
    closes: list[Decimal] = Field(min_length=2, max_length=21)
    flows: FlowBreakdown

    @model_validator(mode="after")
    def validate_box(self) -> WindowMetrics:
        if self.box_high <= self.box_low:
            raise ValueError("box_high must be greater than box_low")
        if len(self.closes) != self.days:
            raise ValueError("closes length must match days")
        return self


class NewsItem(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    source: str = Field(min_length=1, max_length=80)
    published_at: datetime
    url: HttpUrl
    sentiment: Sentiment = "NEUTRAL"


class CandidateView(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")
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
    model_id: str
    prompt_version: str
    is_demo: bool = False
    candidates: list[CandidateView] = Field(min_length=30, max_length=30)

    @model_validator(mode="after")
    def validate_candidate_set(self) -> DashboardReport:
        codes = [candidate.code for candidate in self.candidates]
        if len(set(codes)) != 30:
            raise ValueError("candidate codes must be unique")
        windows: tuple[WindowKey, WindowKey, WindowKey] = ("7", "14", "21")
        for window in windows:
            ranks = [candidate.windows[window].rank for candidate in self.candidates]
            if sorted(ranks) != list(range(1, 31)):
                raise ValueError(f"candidate ranks for {window} days must be 1 through 30")
        return self
