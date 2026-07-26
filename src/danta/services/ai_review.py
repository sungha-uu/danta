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
    report_codes = {item.code for item in report.candidates}
    if set(review_map) != report_codes:
        raise ValueError("AI review must cover every official candidate exactly once")

    candidates = []
    for candidate in report.candidates:
        candidate_review = review_map[candidate.code]
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
    for key in ("7", "14", "21"):
        ready = [
            candidate
            for candidate in candidates
            if candidate.windows[key].structure_status == "READY"
        ]
        if not ready or not all(
            candidate.windows[key].active_box is not None
            for candidate in ready
        ):
            continue
        ranked = sorted(
            ready,
            key=lambda candidate: _active_discovery_rank_key(candidate, key),
            reverse=True,
        )
        rank_map = {
            candidate.code: rank
            for rank, candidate in enumerate(ranked, start=1)
        }
        candidates = [
            candidate.model_copy(
                update={
                    "windows": {
                        **candidate.windows,
                        key: candidate.windows[key].model_copy(
                            update={"rank": rank_map.get(candidate.code)}
                        ),
                    }
                }
            )
            for candidate in candidates
        ]
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
) -> tuple[bool, bool, bool, bool, Decimal]:
    metrics = candidate.windows[key]
    active = metrics.active_box
    if active is None or metrics.structure_status != "READY":
        return (False, False, False, False, Decimal("-1"))
    active_valid = candidate.current_price >= active.structural_invalidation_price
    lower_zone = (
        active_valid
        and active.lower_zone_low
        <= candidate.current_price
        <= active.lower_zone_high
    )
    smart_money_inflow = metrics.flows.foreign + metrics.flows.institution > 0
    recommended = metrics.ai_grade in {"STRONG_RECOMMEND", "RECOMMEND"}
    return (
        active_valid,
        lower_zone,
        smart_money_inflow,
        recommended,
        metrics.final_score or Decimal("0"),
    )
