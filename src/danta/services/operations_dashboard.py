# ruff: noqa: E501
from __future__ import annotations

import json
from html import escape
from pathlib import Path

from danta.services.system_health import OperationsHealthReport


def build_operations_dashboard(
    report: OperationsHealthReport,
    output_dir: Path,
) -> Path:
    rows = "".join(_row_html(item.model_dump(mode="json")) for item in report.rows)
    generated = report.generated_at.strftime("%Y-%m-%d %H:%M:%S")
    html = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Danta 통합 운영 현황</title><style>
:root{{--navy:#111b2b;--line:#d9e0ea;--bg:#f3f6fa}}*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:#152036;font-family:Arial,'Malgun Gothic',sans-serif}}
header{{background:var(--navy);color:white;padding:24px 30px}}header h1{{margin:0 0 8px;font-size:30px}}
main{{max-width:1650px;margin:auto;padding:22px}}.summary{{display:flex;gap:12px;margin-bottom:18px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 18px;min-width:160px}}
.card b{{font-size:26px}}table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line)}}
th{{background:#2d4172;color:#fff;padding:12px;white-space:nowrap}}td{{padding:12px;border-bottom:1px solid var(--line);text-align:center}}
td:nth-child(2),td:nth-child(4),td:nth-child(7){{text-align:left}}.badge{{display:inline-block;border-radius:18px;padding:6px 12px;font-weight:700}}
.normal{{background:#e4f4e9;color:#1e6d3d}}.warning{{background:#fff3cf;color:#8b6200}}.error{{background:#fde1df;color:#a22d28}}
a{{color:#2d5faf;font-weight:700}}.note{{color:#68748b;font-size:13px;margin-top:14px}}
@media(max-width:900px){{main{{padding:10px}}.table-wrap{{overflow:auto}}table{{min-width:1200px}}}}
</style></head><body><header><h1>Danta 통합 운영 현황</h1>
<div>안전 코어부터 연결된 공개 대시보드까지 한 화면에서 확인합니다. · 기준 {generated}</div></header>
<main><section class="summary"><div class="card">정상<br><b>{report.normal_count}</b></div>
<div class="card">확인 필요<br><b>{report.attention_count}</b></div></section>
<div class="table-wrap"><table><thead><tr><th>번호</th><th>시스템</th><th>상태</th><th>현재 작업</th><th>최근 성공</th><th>다음 실행</th><th>문제·조치</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<p class="note">계좌번호·증권사·주문번호·승인문·비밀정보는 이 공개 페이지에 포함하지 않습니다. 표시된 문제는 다음 갱신 때 다시 자체 점검합니다.</p>
</main></body></html>"""
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "index.html"
    temporary = output_dir / ".index.html.tmp"
    temporary.write_text(html, encoding="utf-8")
    temporary.replace(target)
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    payload = report.model_dump(mode="json")
    (output_dir / "health.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def _row_html(item: dict[str, object]) -> str:
    status = str(item["status"])
    css = "normal" if status == "정상" else "warning" if status == "주의" else "error"
    url = item.get("dashboard_url")
    number = int(str(item["number"]))
    name = escape(str(item["name"]))
    name_cell = (
        f'<a href="{escape(str(url))}">{name}</a>'
        if url and number in {3, 4, 5, 6, 7}
        else name
    )
    return (
        "<tr>"
        f"<td>{escape(str(item['number']))}</td>"
        f"<td>{name_cell}</td>"
        f'<td><span class="badge {css}">{escape(status)}</span></td>'
        f"<td>{escape(str(item['current_work']))}</td>"
        f"<td>{escape(str(item['last_success']))}</td>"
        f"<td>{escape(str(item['next_run']))}</td>"
        f"<td>{escape(str(item['issue']) or '-')}</td></tr>"
    )
