from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from danta.dashboard.demo import demo_report
from danta.dashboard.models import ActiveBoxMetrics
from danta.services.ai_review import (
    AiReviewBatch,
    _active_discovery_rank_key,
    apply_ai_review,
)


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


def test_active_discovery_rank_prioritizes_valid_lower_zone_and_inflow() -> None:
    candidate = demo_report().candidates[0]
    metrics = candidate.windows["7"]
    price = candidate.current_price
    active = ActiveBoxMetrics(
        start_date="20260715",
        trading_days=7,
        lower_zone_low=price - Decimal("100"),
        lower_zone_high=price + Decimal("100"),
        upper_zone_low=price + Decimal("1000"),
        upper_zone_high=price + Decimal("1200"),
        position_pct=Decimal("10"),
        amplitude_pct=Decimal("8"),
        upside_to_upper_pct=Decimal("5"),
        inclusion_pct=Decimal("80"),
        lower_contacts=1,
        upper_reaches=1,
        stop_first=0,
        timeouts=0,
        pending=0,
        completed_cycles=0,
        success_rate_pct=Decimal("100"),
        stop_first_rate_pct=Decimal("0"),
        rebound_trend="표본 부족",
        confidence="LOW",
        flow_confirmation="순유입",
        structural_invalidation_price=price - Decimal("200"),
    )
    inflow_metrics = metrics.model_copy(
        update={
            "active_box": active,
            "flows": metrics.flows.model_copy(
                update={
                    "foreign": Decimal("10"),
                    "institution": Decimal("10"),
                }
            ),
        }
    )
    outflow_metrics = inflow_metrics.model_copy(
        update={
            "flows": inflow_metrics.flows.model_copy(
                update={
                    "foreign": Decimal("-10"),
                    "institution": Decimal("-10"),
                }
            )
        }
    )
    inflow = candidate.model_copy(
        update={"windows": {**candidate.windows, "7": inflow_metrics}}
    )
    outflow = candidate.model_copy(
        update={"windows": {**candidate.windows, "7": outflow_metrics}}
    )

    assert _active_discovery_rank_key(inflow, "7") > _active_discovery_rank_key(
        outflow,
        "7",
    )
