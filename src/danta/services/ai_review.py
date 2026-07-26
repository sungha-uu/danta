from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field, HttpUrl, model_validator

from danta.dashboard.models import AiGrade, DashboardReport, NewsItem, Sentiment, WindowKey


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
    candidates: list[AiCandidateReview] = Field(min_length=1, max_length=30)


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
