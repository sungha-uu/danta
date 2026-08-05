from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from danta.config import AppSettings

KST = ZoneInfo("Asia/Seoul")
HealthLevel = Literal["정상", "주의", "오류", "중지", "미구현"]


class SystemHealthRow(BaseModel):
    number: int
    name: str
    status: HealthLevel
    current_work: str
    last_success: str
    next_run: str
    issue: str
    dashboard_url: str | None = None


class OperationsHealthReport(BaseModel):
    schema_version: str = "danta-operations-health-v1"
    generated_at: datetime
    normal_count: int
    attention_count: int
    rows: list[SystemHealthRow]


def collect_operations_health(
    settings: AppSettings,
    *,
    now: datetime | None = None,
) -> OperationsHealthReport:
    current = (now or datetime.now(KST)).astimezone(KST)
    runtime = _read_json(settings.autonomous_campaign_path.parent / "runtime_state.json")
    runtime_updated = _parse_datetime(runtime.get("updated_at"))
    runtime_fresh = runtime_updated is not None and current - runtime_updated < timedelta(
        minutes=3
    )
    database = _database_counts(settings.database_url)
    market = _read_json(Path("../danta_market_status/data/market-status.json"))
    market_at = _parse_datetime(market.get("generated_at") or market.get("observed_at"))
    intraday_window = 8 * 60 + 45 <= current.hour * 60 + current.minute <= 16 * 60
    market_fresh = market_at is not None and (
        current - market_at < timedelta(minutes=10)
        or (
            not intraday_window
            and market_at.date() == current.date()
            and market_at.hour * 60 + market_at.minute >= 15 * 60 + 25
        )
    )
    daily_success = _latest_json(settings.daily_run_root, "*success*.json")
    candidate_at = _file_time(settings.autonomous_report_path)
    performance_at = _latest_report_time(settings.daily_close_root / "reports") or _file_time(
        Path("data/public-performance/latest.json")
    )
    financial_analysis_at = _file_time(
        settings.financial_analysis_dashboard_index_path
    )
    campaign = _read_json(settings.autonomous_campaign_path)
    campaign_active = bool(campaign) and not settings.autonomous_kill_switch_path.exists()
    managed_positions = _as_list(runtime.get("managed_positions"))
    pending_orders = _as_list(runtime.get("pending_orders"))
    risk = market.get("risk")
    risk_level = (
        str(risk.get("level", "상태 확인 중"))
        if isinstance(risk, dict)
        else str(market.get("status", "상태 확인 중"))
    )

    runtime_issue = "" if runtime_fresh else "런타임 상태 갱신이 3분 이상 지연됨"
    fill_issue = ""
    core_status: HealthLevel = "정상" if runtime_fresh else "주의"
    market_status: HealthLevel = "정상" if market_fresh else "주의"
    trade_status: HealthLevel = "정상" if runtime_fresh and campaign_active else "주의"
    candidate_status: HealthLevel = "정상" if candidate_at else "오류"
    performance_status: HealthLevel = "정상" if performance_at else "주의"
    financial_analysis_fresh = (
        financial_analysis_at is not None
        and current - financial_analysis_at < timedelta(days=2)
    )
    financial_analysis_status: HealthLevel = (
        "정상"
        if financial_analysis_fresh
        else "오류"
        if financial_analysis_at is None
        else "주의"
    )

    rows = [
        SystemHealthRow(
            number=1,
            name="통합 운영·안전 코어",
            status=core_status,
            current_work=(
                f"런타임 {runtime.get('orchestrator_state', 'UNKNOWN')} · "
                f"체결 {database['fills']}건"
            ),
            last_success=_format_time(runtime_updated),
            next_run="상시",
            issue=" · ".join(part for part in (runtime_issue, fill_issue) if part),
            dashboard_url=settings.operations_dashboard_public_url,
        ),
        SystemHealthRow(
            number=2,
            name="자율 매매 시스템",
            status=trade_status,
            current_work=(
                f"보유 {len(managed_positions)} · 대기주문 {len(pending_orders)}"
            ),
            last_success=_format_time(runtime_updated),
            next_run="상시 · 시간대별 KRX/NXT 자동 전환",
            issue="" if campaign_active else "자율매매 캠페인이 중지 또는 만료됨",
        ),
        SystemHealthRow(
            number=3,
            name="시장 센싱 시스템",
            status=market_status,
            current_work=(
                risk_level
                if intraday_window
                else "장 마감 스냅샷 보관"
            ),
            last_success=_format_time(market_at),
            next_run="장중 30초 수집 · 공개판 5분",
            issue="" if market_fresh else "시장 데이터가 10분 이상 갱신되지 않음",
            dashboard_url=settings.market_dashboard_public_url,
        ),
        SystemHealthRow(
            number=4,
            name="단기 후보 대시보드",
            status=candidate_status,
            current_work="시총 상위 200 · 14일 고정순위 · 상위 50 AI 심층검토",
            last_success=_format_time(candidate_at),
            next_run="거래일 16:00",
            issue="" if daily_success else "최근 16시 완료 표식 없음; 재실행·검증 필요",
            dashboard_url=settings.dashboard_public_url,
        ),
        SystemHealthRow(
            number=5,
            name="KOSPI 시장·자금 흐름 대시보드",
            status=market_status,
            current_work="KOSPI·투자자·프로그램·시장폭 요약",
            last_success=_format_time(market_at),
            next_run="08:50~15:30, 5분 공개",
            issue="" if market_fresh else "최근 공개 데이터 지연",
            dashboard_url=settings.market_dashboard_public_url,
        ),
        SystemHealthRow(
            number=6,
            name="자율 매매 시스템 실적 대시보드",
            status=performance_status,
            current_work="5백만원 단일 기준 · 보유·체결·누적수익률 공개 요약",
            last_success=_format_time(performance_at),
            next_run="15분 지연 · 장마감 확정",
            issue="" if performance_at else "공개 실적 스냅샷 최초 생성 필요",
            dashboard_url=settings.performance_dashboard_public_url,
        ),
        SystemHealthRow(
            number=7,
            name="AI 종목 추천 및 재무제표",
            status=financial_analysis_status,
            current_work="KOSPI 재무제표 분석 · 적정가 · AI 종목 추천",
            last_success=_format_time(financial_analysis_at),
            next_run="독립 프로젝트의 일일 보고서 갱신",
            issue=(
                ""
                if financial_analysis_fresh
                else "재무제표 대시보드 파일이 없음"
                if financial_analysis_at is None
                else "재무제표 대시보드 갱신이 2일 이상 지연됨"
            ),
            dashboard_url=settings.financial_analysis_dashboard_public_url,
        ),
    ]
    normal = sum(row.status == "정상" for row in rows)
    return OperationsHealthReport(
        generated_at=current,
        normal_count=normal,
        attention_count=len(rows) - normal,
        rows=rows,
    )


def _database_counts(database_url: str) -> dict[str, int]:
    if not database_url.startswith("sqlite"):
        return {"fills": 0}
    path = database_url.rsplit("///", 1)[-1]
    try:
        with sqlite3.connect(path) as connection:
            fills = int(connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0])
    except (OSError, sqlite3.Error):
        fills = 0
    return {"fills": fills}


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(KST) if parsed.tzinfo else parsed.replace(tzinfo=KST)
    except ValueError:
        return None


def _file_time(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=KST)
    except OSError:
        return None


def _latest_json(root: Path, pattern: str) -> Path | None:
    paths = list(root.glob(pattern)) if root.exists() else []
    return max(paths, key=lambda path: path.stat().st_mtime) if paths else None


def _latest_report_time(root: Path) -> datetime | None:
    path = _latest_json(root, "*.json")
    return _file_time(path) if path else None


def _format_time(value: datetime | None) -> str:
    return "기록 없음" if value is None else value.strftime("%m-%d %H:%M:%S")


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []
