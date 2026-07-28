from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from danta.adapters.kis.client import KisClient
from danta.adapters.krx.client import PykrxMarketDataClient
from danta.config import (
    AppSettings,
    load_dart_api_key,
    load_kis_credentials,
    load_krx_environment,
)
from danta.dashboard.builder import build_dashboard
from danta.services.ai_review import apply_ai_review
from danta.services.context_review import PublicContextCollector, build_context_review
from danta.services.intraday_report import (
    MinuteBarStore,
    backfill_minute_bars,
    build_intraday_report,
    market_cap_top_universe,
)


@dataclass(frozen=True, slots=True)
class DailyPipelineResult:
    report_path: Path
    dashboard_path: Path
    candidate_count: int
    deep_review_count: int
    data_as_of: datetime


async def run_daily_pipeline(
    settings: AppSettings,
    *,
    data_root: Path,
    report_output: Path,
    review_output: Path,
    dashboard_output: Path,
    context_cache_root: Path,
    refresh_context: bool = True,
    progress: Callable[[str], None] | None = None,
) -> DailyPipelineResult:
    """Run the local, resumable KOSPI 200 -> quant 200 -> context top 50 cycle."""
    emit = progress if progress is not None else lambda _message: None
    load_krx_environment(settings)
    emit("collecting KRX 21-trading-day dataset")
    dataset = PykrxMarketDataClient().collect(required_days=21)
    universe = market_cap_top_universe(dataset, limit=200)
    if len(universe) != 200:
        raise RuntimeError(f"expected 200 market-cap symbols, got {len(universe)}")
    credentials = load_kis_credentials(settings)
    store = MinuteBarStore(data_root)
    async with KisClient(
        credentials,
        token_cache_path=Path("data/kis-token-cache.json"),
    ) as client:
        await backfill_minute_bars(
            client,
            store,
            universe,
            dataset.trading_dates,
            window_days=21,
            progress=emit,
        )
    quantitative = build_intraday_report(dataset, universe, store)
    if len(quantitative.candidates) != 200:
        raise RuntimeError("quantitative report did not preserve the 200-symbol universe")
    top_50 = sorted(
        quantitative.candidates,
        key=lambda candidate: candidate.windows["14"].rank or 999,
    )[:50]
    emit("collecting news, DART disclosures, and discussions for fixed top 50")
    snapshots = await PublicContextCollector(
        context_cache_root,
        dart_api_key=load_dart_api_key(settings),
    ).collect(
        [(candidate.code, candidate.name) for candidate in top_50],
        refresh=refresh_context,
    )
    review = build_context_review(
        quantitative,
        snapshots,
        reviewed_at=datetime.now().astimezone(),
    )
    reviewed = apply_ai_review(quantitative, review)
    _write_model(report_output, reviewed.model_dump(mode="json"))
    _write_model(review_output, review.model_dump(mode="json"))
    dashboard_path = build_dashboard(reviewed, dashboard_output)
    return DailyPipelineResult(
        report_path=report_output,
        dashboard_path=dashboard_path,
        candidate_count=len(reviewed.candidates),
        deep_review_count=len(review.candidates),
        data_as_of=reviewed.data_as_of,
    )


def _write_model(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
