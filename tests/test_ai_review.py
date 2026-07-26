from __future__ import annotations

from datetime import datetime

import pytest

from danta.dashboard.demo import demo_report
from danta.services.ai_review import AiReviewBatch, apply_ai_review


def test_ai_review_requires_complete_candidate_coverage() -> None:
    report = demo_report()
    review = AiReviewBatch.model_validate(
        {
            "model_id": "codex-test",
            "prompt_version": "test-v1",
            "report_data_as_of": report.data_as_of,
            "reviewed_at": datetime.now(report.data_as_of.tzinfo),
            "candidates": [
                {
                    "code": report.candidates[0].code,
                    "discussion_summary": "테스트 검토",
                    "windows": {
                        key: {
                            "ai_grade": "RECOMMEND",
                            "ai_score": 70,
                            "ai_comment": "구조화된 테스트 검토입니다.",
                            "reasons": ["테스트 근거"],
                            "risks": ["테스트 위험"],
                        }
                        for key in ("7", "14", "21")
                    },
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="every official candidate"):
        apply_ai_review(report, review)
