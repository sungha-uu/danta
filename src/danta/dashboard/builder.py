from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from danta.dashboard.models import DashboardReport


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
