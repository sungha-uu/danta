from __future__ import annotations

import json
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from danta.dashboard.models import DashboardReport

TEN_PCT = Decimal("1.10")
RECOMMENDED_GRADES = {"STRONG_RECOMMEND", "RECOMMEND"}


def _validate_actual_ten_pct_dashboard(report: DashboardReport) -> None:
    if not any(
        version in report.calculation_version
        for version in ("actual-10pct-gate", "period-lower-entry-gate")
    ):
        return
    errors: list[str] = []
    for window in ("7", "14", "21"):
        eligible_ranks: list[int] = []
        ineligible_ranks: list[int] = []
        for candidate in report.candidates:
            metrics = candidate.windows[window]
            if metrics.structure_status != "READY":
                continue
            if (
                metrics.box_low is None
                or metrics.box_high is None
                or metrics.position_pct is None
                or metrics.target_price_10pct is None
                or metrics.target_reach_count is None
                or metrics.current_vs_window_high_pct is None
            ):
                errors.append(f"{window}d {candidate.code} missing +10% audit fields")
                continue
            actual_high = max(bar.high for bar in metrics.chart_bars)
            current_target = candidate.current_price * TEN_PCT
            entry_target = metrics.box_low * TEN_PCT
            target_consistent = abs(metrics.target_price_10pct - entry_target) <= Decimal("0.02")
            ten_pct_evidence = (
                metrics.target_reach_count >= 1
                and actual_high >= current_target
                and target_consistent
            )
            current_in_lower_zone = (
                metrics.position_pct is not None
                and metrics.position_pct <= Decimal("35")
            )
            qualifies = (
                ten_pct_evidence
                and current_in_lower_zone
                and metrics.target_price_10pct > candidate.current_price
                and not any(
                    "원시세 복귀형 급등 소멸" in risk
                    for risk in metrics.risks
                )
            )
            if metrics.rank is not None:
                (eligible_ranks if qualifies else ineligible_ranks).append(metrics.rank)
            if metrics.ai_grade in RECOMMENDED_GRADES and not qualifies:
                errors.append(
                    f"{window}d {candidate.code} recommended without actual +10% evidence"
                )
            expected_vs_high = min(
                Decimal("0"),
                (candidate.current_price / actual_high - Decimal("1")) * Decimal("100"),
            )
            if (
                abs(metrics.current_vs_window_high_pct - expected_vs_high)
                > Decimal("0.02")
            ):
                errors.append(
                    f"{window}d {candidate.code} window-high percentage mismatch"
                )
            if not target_consistent:
                errors.append(
                    f"{window}d {candidate.code} +10% target price mismatch"
                )
            expected_position = (
                (candidate.current_price - metrics.box_low)
                / (metrics.box_high - metrics.box_low)
                * Decimal("100")
            )
            if abs(metrics.position_pct - expected_position) > Decimal("0.02"):
                errors.append(
                    f"{window}d {candidate.code} period-box position mismatch"
                )
        if (
            eligible_ranks
            and ineligible_ranks
            and max(eligible_ranks) > min(ineligible_ranks)
        ):
            errors.append(f"{window}d ineligible candidate ranked ahead of eligible candidate")
    if errors:
        raise ValueError("dashboard +10% invariant failed: " + "; ".join(errors[:10]))


def load_dashboard_report(path: Path) -> DashboardReport:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    return DashboardReport.model_validate(value)


def _asset(name: str) -> str:
    return resources.files("danta.dashboard.assets").joinpath(name).read_text(encoding="utf-8")


def _safe_json(report: DashboardReport) -> str:
    payload = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        payload.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def build_dashboard(report: DashboardReport, output_dir: Path) -> Path:
    _validate_actual_ten_pct_dashboard(report)
    template = _asset("template.html")
    html = (
        template.replace("/*__DANTA_CSS__*/", _asset("report.css"))
        .replace("/*__DANTA_JS__*/", _asset("report.js"))
        .replace("__DANTA_REPORT_JSON__", _safe_json(report))
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "index.html"
    temporary = output_dir / "index.html.tmp"
    temporary.write_text(html, encoding="utf-8", newline="")
    temporary.replace(target)
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    return target
