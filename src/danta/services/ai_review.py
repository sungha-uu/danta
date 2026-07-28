from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

from danta.dashboard.models import (
    AiGrade,
    CandidateView,
    DashboardReport,
    NewsItem,
    Sentiment,
    WindowKey,
)


class AiWindowReview(BaseModel):
    ai_grade: AiGrade
    ai_score: int = Field(ge=0, le=100)
    ai_comment: str = Field(min_length=1, max_length=400)
    reasons: list[str] = Field(min_length=1, max_length=5)
    risks: list[str] = Field(min_length=1, max_length=5)


class AiNewsReview(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    source: str = Field(min_length=1, max_length=80)
    published_at: datetime
    url: HttpUrl
    sentiment: Sentiment = "NEUTRAL"


class AiCandidateReview(BaseModel):
    code: str = Field(pattern=r"^[0-9A-Z]{6}$")
    discussion_summary: str = Field(min_length=1, max_length=500)
    windows: dict[WindowKey, AiWindowReview]
    news: list[AiNewsReview] = Field(default_factory=list, max_length=5)
    discussion_titles: list[str] = Field(default_factory=list, max_length=10)
    discussion_url: HttpUrl | None = None
    context_status: Literal["NOT_COLLECTED", "READY", "PARTIAL", "FAILED"] = (
        "NOT_COLLECTED"
    )

    @model_validator(mode="after")
    def validate_windows(self) -> AiCandidateReview:
        if set(self.windows) != {"7", "14", "21"}:
            raise ValueError("AI review windows must contain 7, 14, and 21")
        return self


class AiReviewBatch(BaseModel):
    model_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    report_data_as_of: datetime
    reviewed_at: datetime
    candidates: list[AiCandidateReview] = Field(min_length=1, max_length=50)


def load_ai_review(path: Path) -> AiReviewBatch:
    return AiReviewBatch.model_validate_json(path.read_text(encoding="utf-8"))


def apply_ai_review(report: DashboardReport, review: AiReviewBatch) -> DashboardReport:
    if review.report_data_as_of != report.data_as_of:
        raise ValueError("AI review data timestamp does not match report")
    review_map = {item.code: item for item in review.candidates}
    review_target_codes = {
        candidate.code
        for candidate in report.candidates
        if candidate.windows["14"].rank is not None
        and candidate.windows["14"].rank <= 50
    }
    if set(review_map) != review_target_codes:
        raise ValueError("AI review must cover every official candidate exactly once")

    candidates = []
    for candidate in report.candidates:
        candidate_review = review_map.get(candidate.code)
        if candidate_review is None:
            candidates.append(
                candidate.model_copy(
                    update={
                        "windows": {
                            key: metrics.model_copy(
                                update={
                                    "ai_grade": None,
                                    "ai_score": None,
                                    "final_score": None,
                                    "ai_comment": None,
                                }
                            )
                            for key, metrics in candidate.windows.items()
                        },
                        "news": [],
                        "discussion_summary": "AI 심층검토 대상 외",
                        "discussion_titles": [],
                        "discussion_url": None,
                        "context_status": "NOT_COLLECTED",
                        "context_fetched_at": None,
                    }
                )
            )
            continue
        windows = {}
        for key, metrics in candidate.windows.items():
            if metrics.structure_status == "WARMING_UP":
                windows[key] = metrics
                continue
            window_review = candidate_review.windows[key]
            ai_score = Decimal(window_review.ai_score)
            windows[key] = metrics.model_copy(
                update={
                    "ai_grade": window_review.ai_grade,
                    "ai_score": ai_score,
                    "final_score": (
                        metrics.quant_score + ai_score
                    ) / 2
                    if metrics.quant_score is not None
                    else ai_score,
                    "ai_comment": window_review.ai_comment,
                    "reasons": window_review.reasons,
                    "risks": window_review.risks,
                }
            )
        candidates.append(
            candidate.model_copy(
                update={
                    "windows": windows,
                    "news": [
                        NewsItem.model_validate(item.model_dump())
                        for item in candidate_review.news
                    ],
                    "discussion_summary": candidate_review.discussion_summary,
                    "discussion_titles": candidate_review.discussion_titles,
                    "discussion_url": candidate_review.discussion_url,
                    "context_status": candidate_review.context_status,
                    "context_fetched_at": review.reviewed_at,
                }
            )
        )
    return report.model_copy(
        update={
            "generated_at": review.reviewed_at,
            "model_id": review.model_id,
            "prompt_version": review.prompt_version,
            "candidates": candidates,
        }
    )


def _active_discovery_rank_key(
    candidate: CandidateView,
    key: WindowKey,
) -> tuple[bool, bool, bool, bool, bool, bool, Decimal]:
    metrics = candidate.windows[key]
    if metrics.structure_status != "READY":
        return (False, False, False, False, False, False, Decimal("-1"))
    current_10pct_threshold = (
        Decimal("1") / Decimal("1.10") - Decimal("1")
    ) * Decimal("100")
    actual_10pct_reached = (
        (metrics.target_reach_count or 0) >= 1
        and metrics.current_vs_window_high_pct is not None
        and metrics.current_vs_window_high_pct <= current_10pct_threshold
    )
    lower_zone = (
        metrics.position_pct is not None
        and metrics.position_pct <= Decimal("35")
    )
    target_above_current = (
        metrics.target_price_10pct is not None
        and metrics.target_price_10pct > candidate.current_price
    )
    smart_money_inflow = metrics.flows.foreign + metrics.flows.institution > 0
    recommended = metrics.ai_grade in {"STRONG_RECOMMEND", "RECOMMEND"}
    expired_spike = any(
        "원시세 복귀형 급등 소멸" in risk for risk in metrics.risks
    )
    return (
        not expired_spike,
        actual_10pct_reached,
        lower_zone,
        target_above_current,
        smart_money_inflow,
        recommended,
        metrics.final_score or Decimal("0"),
    )
