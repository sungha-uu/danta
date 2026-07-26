from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from danta.dashboard.builder import (
    _validate_actual_ten_pct_dashboard,
    build_dashboard,
)
from danta.dashboard.demo import _target_reach_episodes, demo_report
from danta.dashboard.models import DashboardReport


def test_demo_report_has_fifty_ranked_and_graded_candidates_for_every_window() -> None:
    report = demo_report()

    assert report.strategy_status == "RESEARCH_ONLY"
    assert len(report.candidates) == 50
    assert len(report.extended_watchlist) == 0
    for window in ("7", "14", "21"):
        metrics = [candidate.windows[window] for candidate in report.candidates]
        assert sorted(item.rank for item in metrics) == list(range(1, 51))
        assert all(len(item.closes) == item.days for item in metrics)
        assert all(
            item.ai_grade
            in {
                "STRONG_RECOMMEND",
                "RECOMMEND",
                "NOT_RECOMMEND",
                "STRONG_NOT_RECOMMEND",
            }
            for item in metrics
        )
        assert all(item.target_reach_count >= 0 for item in metrics)


def test_actual_ten_pct_dashboard_blocks_false_recommendations() -> None:
    report = demo_report().model_copy(
        update={
            "calculation_version": "intraday-elasticity-v8-actual-10pct-gate-v1"
        }
    )
    candidate = report.candidates[0]
    metrics = candidate.windows["7"].model_copy(
        update={
            "ai_grade": "RECOMMEND",
            "target_reach_count": 0,
        }
    )
    report = report.model_copy(
        update={
            "candidates": [
                candidate.model_copy(
                    update={
                        "windows": {
                            **candidate.windows,
                            "7": metrics,
                        }
                    }
                ),
                *report.candidates[1:],
            ]
        }
    )

    with pytest.raises(ValueError, match="recommended without actual \\+10% evidence"):
        _validate_actual_ten_pct_dashboard(report)
        assert all(item.median_daily_range_pct >= 0 for item in metrics)
        assert all(item.max_daily_rebound_pct >= item.median_daily_rebound_pct for item in metrics)
        assert all(len(item.chart_bars) == len(item.closes) for item in metrics)
        assert all(
            item.lower_contact_count
            == item.target_reach_count
            + item.target_pending_count
            + item.target_expired_count
            for item in metrics
        )


def test_active_report_requires_intraday_source_and_approved_analysis_bar() -> None:
    payload = demo_report().model_dump(mode="json")
    payload["strategy_status"] = "ACTIVE"

    with pytest.raises(ValidationError, match="1-minute source"):
        DashboardReport.model_validate(payload)

    payload["source_bar_interval_minutes"] = 1
    payload["analysis_bar_interval_minutes"] = 60
    report = DashboardReport.model_validate(payload)

    assert report.strategy_status == "ACTIVE"


def test_warming_window_requires_incomplete_structure_days() -> None:
    payload = demo_report().model_dump(mode="json")
    for candidate in payload["candidates"][-1:]:
        candidate["windows"]["14"]["structure_status"] = "WARMING_UP"
        candidate["windows"]["14"]["structure_completed_days"] = 10
        for field in (
            "rank",
            "box_low",
            "box_high",
            "amplitude_pct",
            "position_pct",
            "median_daily_range_pct",
            "max_daily_range_pct",
            "median_daily_rebound_pct",
            "max_daily_rebound_pct",
            "reach_days_5pct",
            "reach_days_10pct",
            "reach_days_15pct",
            "current_vs_window_high_pct",
            "lower_trend_pct",
            "lower_trend",
            "target_price_10pct",
            "lower_contact_count",
                "target_reach_count",
                "target_pending_count",
                "target_expired_count",
            "breakdown_risk_pct",
            "quant_score",
            "ai_score",
            "final_score",
            "ai_grade",
            "ai_comment",
            "invalidation",
        ):
            candidate["windows"]["14"][field] = None
        candidate["windows"]["14"]["reasons"] = []
        candidate["windows"]["14"]["risks"] = []
        candidate["windows"]["14"]["closes"] = []
        candidate["windows"]["14"]["chart_bars"] = []

    report = DashboardReport.model_validate(payload)

    assert report.candidates[-1].windows["14"].structure_completed_days == 10

    payload["candidates"][-1]["windows"]["14"]["structure_completed_days"] = 14
    with pytest.raises(ValidationError, match="WARMING_UP"):
        DashboardReport.model_validate(payload)


def test_target_reach_requires_contact_before_later_target() -> None:
    low = Decimal("100")

    assert _target_reach_episodes([low, Decimal("109")], low) == (1, 0, 1)
    assert _target_reach_episodes([Decimal("110"), low], low) == (1, 0, 1)
    assert _target_reach_episodes([low, Decimal("110")], low) == (1, 1, 0)
    assert _target_reach_episodes(
        [low, Decimal("110"), low, Decimal("111")], low
    ) == (2, 2, 0)


def test_dashboard_build_is_self_contained_and_global_windowed(tmp_path: Path) -> None:
    target = build_dashboard(demo_report(), tmp_path)
    html = target.read_text(encoding="utf-8")

    assert target.name == "index.html"
    assert html.index("const $ =") < html.index('$("#candidateCountTitle")')
    assert (tmp_path / ".nojekyll").exists()
    assert "AI 분석 30" not in html
    assert 'id="searchInput"' not in html
    assert 'class="tabs"' not in html
    assert 'data-window="7"' in html
    assert 'data-window="14"' in html
    assert 'data-window="21"' in html
    assert "item.return_pct" in html
    assert "Math.abs(number)" in html
    assert 'number > 0 ? "+"' not in html
    assert "minimumFractionDigits: 1" in html
    assert "기간수익률" in html
    header_html = html[html.index("<thead>") : html.index("</thead>")]
    expected_headers = [
        "기간수익률",
        ">기간고점 대비<",
        "가격 흐름",
        "진입 기준가",
        "+10% 목표가",
        "기간 최고가",
        "+10% 이력",
        "현재 위치",
        "하단 방향",
        "일중 진폭",
        "저점 반등",
        "도달일수",
    ]
    assert [header_html.index(value) for value in expected_headers] == sorted(
        header_html.index(value) for value in expected_headers
    )
    assert "일중 진폭" in html
    assert "저점 반등" in html
    assert "도달일수" in html
    assert ">기간고점 대비<" in html
    assert "하단 방향" in html
    assert "하단 진입권" in html
    assert "상단권" in html
    assert "왕복</th>" not in html
    assert 'id="chartModal"' in html
    assert "60분봉 상세 보기" in html
    assert "chart_bars" in html
    assert "target_price_10pct" in html
    assert 'id="chartModalHead"' in html
    assert "byDateBucket" in html
    assert "hour-bar-cell" in html
    assert "modal-price-tick" in html
    assert "modal-date-tick" in html
    assert "modal-current-line" in html
    assert "modal-trading-date" in html
    assert "modal-trading-date-head" in html
    assert "z-index: 4" in html
    assert "상단(현재가+10%)" in html
    assert ">박스 하단</text>" in html
    assert ">현재가</text>" in html
    assert "modal-current-label" in html
    assert 'class="modal-reference-legend"' in html
    assert 'aria-label="차트 기준선 범례"' in html
    assert "const labelGutter = 132" in html
    assert "const legendX = plotRight + 8" in html
    assert "lowerIsAboveCurrent" not in html
    assert "target15" not in html
    assert "현재가 ${won.format(" in html
    assert 'id="chartModalSummary"' in html
    assert "<small>량 ${won.format(n(bar.volume))} · 시 ${won.format(n(bar.open))}</small>" in html
    assert "<small>저 ${won.format(n(bar.low))} · 고 ${won.format(n(bar.high))}</small>" in html
    assert "분봉 준비 시 첫 60분봉 종가" in html
    assert (
        ".modal-current-line { stroke: #111827; stroke-width: .8; "
        "stroke-dasharray: 6 4; }"
    ) in html
    assert "연구용 · 주문 불가" in html
    assert "strategy_status" in html
    assert "분봉 수집 중" in html
    assert "structure_status" in html
    assert '"average_trading_value_billion"' in html
    assert "item.flows" in html
    assert "검토 후보 ${candidates.length}" in html
    assert "extended_watchlist" in html
    assert "extended-watch-row" in html
    assert "extended-badge" in html
    assert "AI 코멘트" in html
    assert "정량 등급" in html
    assert "AI 정성 검토는 아직 미연결입니다." in html
    assert "reviewGradeLabel" in html
    assert "최신 뉴스" in html
    assert ">종목토론</th>" in html
    assert ">토론 요약</th>" not in html
    assert "sentiment-positive" in html
    assert "sentiment-negative" in html
    assert "sentiment-neutral" in html
    assert "[${tag.label}]" in html
    assert "discussion_titles" in html
    assert 'class="discussion-title"' in html
    assert "개인(순매수억)" in html
    assert "외국인(순매수억)" in html
    assert "기관(순매수억)" in html
    assert "금융투자(순매수억)" in html
    assert "연기금(순매수억)" in html
    assert "외국인+기관 수급강도(%)" in html
    assert 'id="flowOnly"' not in html
    assert "외국인+기관 순유입" not in html
    assert "DANTA ENTRY_MANDATE" in html
    assert "ENTRY_APPROVAL" in html
    assert "USER_DEFINED_ORDERABLE_CASH_PERCENT" in html
    assert "비율(%)" in html
    assert "total_allocation_pct" in html
    assert "minimumFractionDigits: 1" in html
    assert "Math.floor((100 / selection.size) * 10) / 10" in html
    assert "KIS_ORDERABLE_CASH" in html
    assert "UNTIL_FILLED_OR_BOX_INVALIDATED" in html
    assert "hard_stop_pct: -7.0" in html
    assert "profit_policy: ACTIVE_VERSIONED_LOCAL_ENGINE" in html
    assert "profit_arm_pct" not in html
    assert "entry_target_price_krw" in html
    assert "entry_price_source" in html
    assert "BOX_LOW_AUTO" in html
    assert "USER_EDITED" in html
    assert "review_amount_krw" not in html
    assert "selection.size >= 3" in html
    assert "<th>검토금액</th>" not in html
    assert 'id="selectionTray"' in html
    assert 'id="selectionTray" class="selection-tray" aria-live="polite" hidden' in html
    assert 'class="selection-summary"' in html
    assert "선택 <span id=\"selectionCount\"" in html
    assert 'id="selectionMessage" class="sr-only"' in html
    assert "flex: 0 0 240px" in html
    assert "height: 29px" in html
    assert 'id="toggleSelection"' in html
    assert "선택창 표시" in html
    assert 'aria-label="전체 해제"' in html
    assert 'aria-label="자동매수 승인문 복사"' in html
    assert 'id="copyToast"' in html
    assert "button-spinner" in html
    assert "window.setTimeout(resolve, 1000)" in html
    assert "승인문 복사 완료 · Codex에 붙여넣으세요" in html
    assert 'class="icon-button' in html
    assert "caret-color: transparent" in html
    assert "user-select: none" in html
    assert "function selectedCandidates()" in html
    assert "[...selection.keys()]" in html
    assert ".report-table thead th { text-align: center !important; }" in html
    assert "<th>일평균 거래대금</th>" not in html
    assert "<th>거래량 배율</th>" not in html
    assert ">유동성</th>" not in html
    assert "핵심 위험 / 제외 조건" not in html
    assert "KIS_APP_SECRET" not in html


def test_report_rejects_missing_candidate() -> None:
    payload = demo_report().model_dump(mode="json")
    payload["candidates"].pop()

    with pytest.raises(ValidationError):
        DashboardReport.model_validate(payload)


def test_report_does_not_force_grade_counts() -> None:
    payload = demo_report().model_dump(mode="json")
    for candidate in payload["candidates"]:
        for window in ("7", "14", "21"):
            candidate["windows"][window]["ai_grade"] = "NOT_RECOMMEND"

    report = DashboardReport.model_validate(payload)

    assert all(
        candidate.windows["14"].ai_grade == "NOT_RECOMMEND"
        for candidate in report.candidates
    )


def test_report_json_escapes_script_breakout(tmp_path: Path) -> None:
    payload = demo_report().model_dump(mode="json")
    payload["candidates"][0]["windows"]["14"]["ai_comment"] = "</script><script>alert(1)</script>"
    report = DashboardReport.model_validate(json.loads(json.dumps(payload)))

    html = build_dashboard(report, tmp_path).read_text(encoding="utf-8")

    assert "</script><script>alert(1)</script>" not in html
    assert "\\u003c/script\\u003e" in html
