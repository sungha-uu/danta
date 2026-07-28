from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from danta.adapters.dart.financials import OpenDartFinancialClient
from danta.adapters.kis.client import KisClient
from danta.adapters.krx.client import PykrxMarketDataClient
from danta.config import (
    AppSettings,
    TradingEnvironment,
    load_dart_api_key,
    load_kis_credentials,
    load_krx_environment,
)
from danta.dashboard.builder import build_dashboard
from danta.domain.fundamentals import FundamentalSnapshotBatch
from danta.services.ai_review import apply_ai_review
from danta.services.context_review import PublicContextCollector, build_context_review
from danta.services.fundamental_snapshot import (
    attach_fundamentals,
    load_fundamental_batch,
    refresh_fundamental_snapshots,
    report_candidates_for,
)
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
    fundamental_snapshot_count: int
    fundamental_unavailable_count: int


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
    """Run KOSPI 200 -> official +10% gate (max 30) -> full context review."""
    emit = progress if progress is not None else lambda _message: None
    load_krx_environment(settings)
    emit("collecting KRX 21-trading-day dataset")
    dataset = PykrxMarketDataClient().collect(required_days=21)
    universe = market_cap_top_universe(dataset, limit=200)
    if len(universe) != 200:
        raise RuntimeError(f"expected 200 market-cap symbols, got {len(universe)}")
    emit("refreshing independent Open DART financial snapshots")
    try:
        fundamentals = await refresh_fundamental_snapshots(
            OpenDartFinancialClient(
                load_dart_api_key(settings),
                corp_code_cache_path=settings.dart_corp_code_cache_path,
            ),
            [(item.symbol, item.name) for item in universe],
            output_path=settings.fundamental_snapshot_path,
            as_of=dataset.trading_dates[-1],
            progress=emit,
        )
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
        emit(f"financial snapshot degraded: {type(exc).__name__}: {exc}")
        existing = load_fundamental_batch(settings.fundamental_snapshot_path)
        if existing is not None:
            fundamentals = existing
        else:
            target_year, target_code = report_candidates_for(
                dataset.trading_dates[-1]
            )[0]
            fundamentals = FundamentalSnapshotBatch(
                generated_at=datetime.now().astimezone(),
                target_business_year=target_year,
                target_report_code=target_code,  # type: ignore[arg-type]
                requested_symbols=tuple(item.symbol for item in universe),
                snapshots=(),
                unavailable_symbols=tuple(item.symbol for item in universe),
                provider_errors=(f"{type(exc).__name__}: {exc}",),
            )
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
    quantitative = attach_fundamentals(
        build_intraday_report(
            dataset,
            universe,
            store,
            strategy_status=(
                "ACTIVE"
                if settings.environment is TradingEnvironment.PAPER
                and settings.paper_order_execution_enabled
                else "RESEARCH_ONLY"
            ),
        ),
        fundamentals,
    )
    if len(quantitative.candidates) > 30:
        raise RuntimeError("official candidate report exceeded 30 symbols")
    official_candidates = sorted(
        quantitative.candidates,
        key=lambda candidate: candidate.windows["14"].rank or 999,
    )
    emit("collecting news, DART disclosures, and discussions for all candidates")
    snapshots = await PublicContextCollector(
        context_cache_root,
        dart_api_key=load_dart_api_key(settings),
    ).collect(
        [(candidate.code, candidate.name) for candidate in official_candidates],
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
        fundamental_snapshot_count=len(fundamentals.snapshots),
        fundamental_unavailable_count=len(fundamentals.unavailable_symbols),
    )


def _write_model(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
