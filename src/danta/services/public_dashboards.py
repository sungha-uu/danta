from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from danta.adapters.kis.client import KisClient
from danta.config import AppSettings, load_kis_credentials
from danta.services.operations_dashboard import build_operations_dashboard
from danta.services.public_performance import (
    PublicPerformanceReport,
    build_public_performance_page,
    collect_public_performance,
)
from danta.services.static_pages import publish_static_site
from danta.services.system_health import collect_operations_health


@dataclass(frozen=True)
class PublicDashboardResult:
    operations_path: Path
    performance_path: Path
    operations_commit: str | None
    performance_commit: str | None


async def refresh_public_dashboards(
    settings: AppSettings,
    *,
    publish: bool,
) -> PublicDashboardResult:
    credentials = load_kis_credentials(settings)
    async with KisClient(
        credentials,
        token_cache_path=Path("data/kis-token-cache.json"),
    ) as broker:
        fresh_performance = await collect_public_performance(settings, broker)
    public_data = Path("data/public-performance/latest.json")
    previous_performance = _load_previous_performance(public_data)
    cutoff = fresh_performance.generated_at - timedelta(
        minutes=fresh_performance.delayed_minutes
    )
    performance = (
        previous_performance
        if previous_performance is not None
        and previous_performance.generated_at <= cutoff
        else fresh_performance
    )
    performance_root = Path("dashboard/performance")
    performance_path = build_public_performance_page(
        performance,
        performance_root,
        operations_url=settings.operations_dashboard_public_url,
    )
    public_data.parent.mkdir(parents=True, exist_ok=True)
    public_data.write_text(
        json.dumps(
            fresh_performance.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    health = collect_operations_health(settings)
    operations_root = Path("dashboard/operations")
    operations_path = build_operations_dashboard(health, operations_root)
    operations_commit = None
    performance_commit = None
    if publish:
        performance_commit = publish_static_site(
            source_dir=performance_root,
            repository=settings.performance_dashboard_publish_repo,
            commit_message="Update public autonomous performance dashboard",
        )
        operations_commit = publish_static_site(
            source_dir=operations_root,
            repository=settings.operations_dashboard_publish_repo,
            commit_message="Update integrated operations dashboard",
        )
    return PublicDashboardResult(
        operations_path=operations_path,
        performance_path=performance_path,
        operations_commit=operations_commit,
        performance_commit=performance_commit,
    )


def _load_previous_performance(path: Path) -> PublicPerformanceReport | None:
    try:
        return PublicPerformanceReport.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
