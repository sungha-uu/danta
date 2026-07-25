from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from danta.dashboard.builder import build_dashboard
from danta.dashboard.demo import _traversal_count, demo_report
from danta.dashboard.models import DashboardReport


def test_demo_report_has_thirty_ranked_and_graded_candidates_for_every_window() -> None:
    report = demo_report()

    assert len(report.candidates) == 30
    for window in ("7", "14", "21"):
        metrics = [candidate.windows[window] for candidate in report.candidates]
        assert sorted(item.rank for item in metrics) == list(range(1, 31))
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
        assert all(0 <= item.traversal_count < item.days for item in metrics)

    assert any(
        len(
            {
                candidate.windows["7"].traversal_count,
                candidate.windows["14"].traversal_count,
                candidate.windows["21"].traversal_count,
            }
        )
        > 1
        for candidate in report.candidates
    )


def test_traversal_count_requires_return_to_starting_zone() -> None:
    low = Decimal("0")
    high = Decimal("100")

    assert _traversal_count([low, high], low, high) == 0
    assert _traversal_count([high, low], low, high) == 0
    assert _traversal_count([low, high, low], low, high) == 1
    assert _traversal_count([high, low, high], low, high) == 1
    assert _traversal_count([low, high, low, high, low], low, high) == 2


def test_dashboard_build_is_self_contained_and_global_windowed(tmp_path: Path) -> None:
    target = build_dashboard(demo_report(), tmp_path)
    html = target.read_text(encoding="utf-8")

    assert target.name == "index.html"
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
    assert "선택 기간 첫 거래일 종가 대비 현재가 변화율" in html
    assert '"average_trading_value_billion"' in html
    assert "item.flows" in html
    assert "후보 30 종합" in html
    assert "AI 코멘트" in html
    assert "최신 뉴스" in html
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
