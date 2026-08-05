from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx

from danta.config import AppSettings, load_smtp_config
from danta.services.daily_pipeline import DailyPipelineResult, run_daily_pipeline
from danta.services.notifier import SmtpNotifier

KST = ZoneInfo("Asia/Seoul")


class DailyOperationError(RuntimeError):
    """Raised when a scheduled report cannot be safely published."""


@dataclass(frozen=True, slots=True)
class ScheduledRefreshResult:
    status: Literal[
        "COMPLETED",
        "ALREADY_COMPLETED",
        "NON_TRADING_DAY",
        "DATA_NOT_READY",
    ]
    trade_date: str
    completed_at: str
    detail: str
    dashboard_path: str | None = None
    published_commit: str | None = None


async def run_scheduled_refresh(
    settings: AppSettings,
    *,
    force: bool = False,
    publish: bool | None = None,
    notify: bool | None = None,
    now: datetime | None = None,
    progress: Callable[[str], None] | None = None,
) -> ScheduledRefreshResult:
    current = (now or datetime.now(KST)).astimezone(KST)
    trade_date = current.date().isoformat()
    emit = progress if progress is not None else lambda _message: None
    if current.weekday() >= 5 and not force:
        return ScheduledRefreshResult(
            status="NON_TRADING_DAY",
            trade_date=trade_date,
            completed_at=current.isoformat(),
            detail="weekend refresh skipped",
        )
    settings.daily_run_root.mkdir(parents=True, exist_ok=True)
    marker = settings.daily_run_root / f"{trade_date}-success.json"
    if marker.exists() and not force:
        return ScheduledRefreshResult(
            status="ALREADY_COMPLETED",
            trade_date=trade_date,
            completed_at=current.isoformat(),
            detail="daily success marker already exists",
        )
    lock = settings.daily_run_root / "daily-refresh.lock"
    with _exclusive_lock(lock):
        result = await run_daily_pipeline(
            settings,
            data_root=Path("data/intraday/1m"),
            report_output=settings.autonomous_report_path,
            review_output=Path("data/context-review-latest.json"),
            dashboard_output=Path("dashboard/dist"),
            context_cache_root=Path("data/public-context"),
            refresh_context=True,
            progress=emit,
        )
        if result.data_as_of.astimezone(KST).date() != current.date() and not force:
            not_ready = ScheduledRefreshResult(
                status="DATA_NOT_READY",
                trade_date=trade_date,
                completed_at=datetime.now(KST).isoformat(),
                detail=(
                    "latest market dataset is "
                    f"{result.data_as_of.astimezone(KST).date().isoformat()}"
                ),
                dashboard_path=str(result.dashboard_path),
            )
            _write_run_record(
                settings.daily_run_root / f"{trade_date}-not-ready.json",
                not_ready,
            )
            return not_ready
        should_publish = (
            settings.daily_publish_enabled if publish is None else publish
        )
        commit: str | None = None
        if should_publish:
            emit("publishing static dashboard to GitHub Pages repository")
            commit = publish_dashboard(
                result,
                source_dir=result.dashboard_path.parent,
                report_repo=settings.dashboard_publish_repo,
            )
            await verify_published_report(
                settings.dashboard_public_url,
                expected_data_as_of=result.data_as_of,
            )
        should_notify = settings.daily_notify_enabled if notify is None else notify
        if should_notify:
            if not should_publish:
                raise DailyOperationError(
                    "daily notification requires a verified published report"
                )
            SmtpNotifier(load_smtp_config(settings)).send_stage_completed(
                settings.dashboard_public_url,
                stage=f"{trade_date} 16시 Danta 일일 리포트",
                detail=(
                    f"공식 후보 {result.candidate_count}개, "
                    f"재무 스냅샷 {result.fundamental_snapshot_count}개, "
                    f"재무 미확보 {result.fundamental_unavailable_count}개, "
                    f"추천 평가 스냅샷 {result.performance_snapshot_count}일, "
                    f"평가 상태 {result.recommendation_edge_status}"
                ),
            )
        completed = ScheduledRefreshResult(
            status="COMPLETED",
            trade_date=trade_date,
            completed_at=datetime.now(KST).isoformat(),
            detail=(
                f"{result.candidate_count} candidates; "
                f"{result.fundamental_snapshot_count} fundamentals; "
                f"recommendation performance "
                f"{result.recommendation_edge_status}"
            ),
            dashboard_path=str(result.dashboard_path),
            published_commit=commit,
        )
        _write_run_record(marker, completed)
        return completed


def publish_dashboard(
    result: DailyPipelineResult,
    *,
    source_dir: Path,
    report_repo: Path,
) -> str:
    _run_git(report_repo, "rev-parse", "--is-inside-work-tree")
    dirty = _run_git(report_repo, "status", "--porcelain").strip()
    if dirty:
        raise DailyOperationError(
            "dashboard report repository has uncommitted changes"
        )
    for name in ("index.html", ".nojekyll"):
        source = source_dir / name
        if not source.exists():
            raise DailyOperationError(f"dashboard artifact is missing: {source}")
        temporary = report_repo / f"{name}.tmp"
        shutil.copy2(source, temporary)
        temporary.replace(report_repo / name)
    _run_git(report_repo, "add", "--", "index.html", ".nojekyll")
    staged = subprocess.run(
        ["git", "-C", str(report_repo), "diff", "--cached", "--quiet"],
        check=False,
    )
    if staged.returncode not in {0, 1}:
        raise DailyOperationError("failed to inspect staged dashboard changes")
    if staged.returncode == 1:
        trade_date = result.data_as_of.astimezone(KST).date().isoformat()
        _run_git(
            report_repo,
            "commit",
            "-m",
            f"Update Danta daily report for {trade_date}",
        )
        _run_git(report_repo, "push", "origin", "HEAD:main")
    return _run_git(report_repo, "rev-parse", "HEAD").strip()


async def verify_published_report(
    public_url: str,
    *,
    expected_data_as_of: datetime,
    attempts: int = 12,
    interval_seconds: float = 10,
) -> None:
    expected = expected_data_as_of.isoformat()
    parts = urlsplit(public_url)
    query = urlencode({"verify": expected_data_as_of.timestamp()})
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={"Cache-Control": "no-cache"},
    ) as client:
        for attempt in range(attempts):
            response = await client.get(url)
            if response.status_code == 200 and f'"data_as_of":"{expected}"' in response.text:
                return
            if attempt + 1 < attempts:
                await asyncio.sleep(interval_seconds)
    raise DailyOperationError(
        "GitHub Pages did not expose the expected report data timestamp"
    )


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield
    except FileExistsError as exc:
        raise DailyOperationError("daily refresh is already running") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
            path.unlink(missing_ok=True)


def _run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DailyOperationError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def _write_run_record(path: Path, result: ScheduledRefreshResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
