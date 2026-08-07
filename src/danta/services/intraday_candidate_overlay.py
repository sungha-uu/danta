from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from danta.adapters.kis.client import KisApiError, KisClient, KisMinuteBar
from danta.config import AppSettings
from danta.dashboard.builder import load_dashboard_report
from danta.services.intraday_report import MinuteBarStore, _analyze_symbol, _score_all
from danta.services.market_session import TradingSessionPhase, trading_session_phase
from danta.services.runtime_repository import SqlRuntimeRepository

KST = ZoneInfo("Asia/Seoul")


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class IntradayCandidateOverlay:
    """Refresh all 200 quantitative candidates without blocking protection tasks."""

    def __init__(self, *, settings: AppSettings, broker: KisClient,
                 repository: SqlRuntimeRepository, store: MinuteBarStore | None = None) -> None:
        self.settings = settings
        self.broker = broker
        self.repository = repository
        self.store = store or MinuteBarStore(Path("data/intraday/1m"))
        self._last_publication_at: datetime | None = None

    async def run(self) -> None:
        while True:
            started = asyncio.get_running_loop().time()
            try:
                now = datetime.now(UTC)
                if trading_session_phase(now) is TradingSessionPhase.KRX_REGULAR:
                    await self.refresh(now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.repository.audit(
                    "INTRADAY_OVERLAY_REFRESH_ERROR", correlation_id="intraday-overlay",
                    payload={"error": type(exc).__name__, "detail": str(exc)[:240]},
                )
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(5.0, self.settings.intraday_overlay_interval_seconds - elapsed))

    async def refresh(self, now: datetime) -> dict[str, object]:
        report = load_dashboard_report(self.settings.autonomous_report_path)
        candidates = [*report.candidates, *report.extended_watchlist]
        if len(candidates) != 200:
            raise ValueError("intraday overlay requires the complete 200-name report")
        local = now.astimezone(KST)
        trading_date = local.strftime("%Y%m%d")
        end_time = min(local.time(), time(15, 30)).strftime("%H%M%S")
        analyses = []
        current_session_volumes: dict[str, int] = {}
        failures: list[str] = []
        delay = float(self.settings.intraday_overlay_request_interval_seconds)
        for candidate in candidates:
            try:
                recent = await self.broker.recent_minute_bars(
                    candidate.code, trading_date=trading_date, end_time=end_time
                )
                existing = self.store.load(candidate.code, trading_date)
                merged = {bar.trading_time: bar for bar in existing}
                merged.update({bar.trading_time: bar for bar in recent})
                today = sorted(merged.values(), key=lambda item: item.trading_time)
                current_session_volumes[candidate.code] = sum(bar.volume for bar in today)
                self.store.save(candidate.code, trading_date, today)
                historical = self._historical_bars(candidate.code, trading_date, days=13)
                analyses.append(_analyze_symbol(candidate.code, [*historical, *today]))
            except (KisApiError, ValueError, OSError):
                failures.append(candidate.code)
            await asyncio.sleep(delay)
        if len(analyses) < 180:
            raise RuntimeError(f"intraday overlay coverage too low: {len(analyses)}/200")
        ranked = _score_all(analyses, {})
        flow_estimates: dict[str, tuple[str, int, int, int, Decimal]] = {}
        for analysis in ranked[:50]:
            try:
                estimate = await self.broker.stock_investor_estimate(analysis.symbol)
                session_volume = current_session_volumes.get(analysis.symbol, 0)
                if session_volume <= 0:
                    raise ValueError("current session volume is not positive")
                strength = (
                    Decimal(estimate.combined_net_quantity)
                    / Decimal(session_volume)
                    * Decimal("100")
                )
                flow_estimates[analysis.symbol] = (
                    estimate.observation_label,
                    estimate.foreign_net_quantity,
                    estimate.institution_net_quantity,
                    estimate.combined_net_quantity,
                    strength,
                )
            except (KisApiError, ValueError):
                failures.append(f"{analysis.symbol}:FLOW")
            await asyncio.sleep(delay)
        by_code = {item.code: item for item in candidates}
        rows: list[dict[str, object]] = []
        for rank, analysis in enumerate(ranked, start=1):
            candidate = by_code[analysis.symbol]
            metrics = candidate.windows["14"]
            flow = flow_estimates.get(analysis.symbol)
            rows.append({
                "symbol": analysis.symbol, "name": candidate.name,
                "live_rank_14d": rank, "live_price": int(analysis.hourly_closes[-1]),
                "live_position_pct": str(analysis.position.quantize(Decimal("0.01"))),
                "box_low": str(analysis.low.quantize(Decimal("0.01"))),
                "box_high": str(analysis.high.quantize(Decimal("0.01"))),
                "target_price_10pct": str(analysis.target_price.quantize(Decimal("0.01"))),
                "amplitude_pct": str(analysis.amplitude.quantize(Decimal("0.01"))),
                "average_up_swing_pct": str(analysis.average_up_swing.quantize(Decimal("0.01"))),
                "up_swing_count": analysis.up_swing_count,
                "average_time_to_6pct_hours": None if analysis.average_time_to_6pct_hours is None
                else str(analysis.average_time_to_6pct_hours.quantize(Decimal("0.01"))),
                "median_daily_range_pct": str(
                    analysis.median_daily_range.quantize(Decimal("0.01"))
                ),
                "max_daily_range_pct": str(
                    analysis.max_daily_range.quantize(Decimal("0.01"))
                ),
                "median_daily_rebound_pct": str(
                    analysis.median_daily_rebound.quantize(Decimal("0.01"))
                ),
                "max_daily_rebound_pct": str(
                    analysis.max_daily_rebound.quantize(Decimal("0.01"))
                ),
                "reach_days_5pct": analysis.reach_days_5,
                "reach_days_10pct": analysis.reach_days_10,
                "reach_days_15pct": analysis.reach_days_15,
                "current_vs_window_high_pct": str(
                    (-analysis.current_to_window_high).quantize(Decimal("0.01"))
                ),
                "discount_from_window_high_pct": str(
                    analysis.current_to_window_high.quantize(Decimal("0.01"))
                ),
                "lower_trend_pct": str(analysis.lower_trend.quantize(Decimal("0.01"))),
                "intraday_flow_status": "READY" if flow is not None else "UNAVAILABLE",
                "intraday_flow_observation": flow[0] if flow is not None else None,
                "intraday_foreign_net_qty": flow[1] if flow is not None else None,
                "intraday_institution_net_qty": flow[2] if flow is not None else None,
                "intraday_combined_net_qty": flow[3] if flow is not None else None,
                "intraday_flow_strength_pct": (
                    str(flow[4].quantize(Decimal("0.01"))) if flow is not None else None
                ),
                "closes": [str(value) for value in analysis.hourly_closes],
                "chart_bars": [
                    {
                        "trading_date": bar.trading_date,
                        "bucket": bar.bucket,
                        "open": str(bar.open),
                        "high": str(bar.high),
                        "low": str(bar.low),
                        "close": str(bar.close),
                        "volume": str(bar.volume),
                    }
                    for bar in analysis.hour_bars
                ],
                "base_rank_14d": metrics.rank, "ai_grade": metrics.ai_grade,
                "context_status": candidate.context_status,
            })
        payload: dict[str, object] = {
            "schema_version": 3, "revision": f"{trading_date}-{local.strftime('%H%M')}",
            "observed_at": now.isoformat(), "report_data_as_of": report.data_as_of.isoformat(),
            "coverage": len(rows), "failed_symbols": failures, "rows": rows,
        }
        _atomic_json(self.settings.intraday_overlay_path, payload)
        await self.repository.audit(
            "INTRADAY_OVERLAY_REFRESHED", correlation_id="intraday-overlay",
            payload={
                "coverage": len(rows),
                "failures": len(failures),
                "revision": payload["revision"],
            },
        )
        await self._publish_public_overlay(payload, now)
        return payload

    async def _publish_public_overlay(
        self,
        payload: dict[str, object],
        now: datetime,
    ) -> None:
        if not self.settings.intraday_overlay_public_enabled:
            return
        if self._last_publication_at is not None:
            elapsed = (now - self._last_publication_at).total_seconds()
            if elapsed < self.settings.intraday_overlay_public_interval_seconds:
                return
        try:
            await asyncio.to_thread(
                _commit_public_overlay,
                self.settings.dashboard_publish_repo,
                payload,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            await self.repository.audit(
                "INTRADAY_OVERLAY_PUBLICATION_ERROR",
                correlation_id="intraday-overlay",
                payload={"error": type(exc).__name__, "detail": str(exc)[:240]},
            )
            return
        self._last_publication_at = now
        await self.repository.audit(
            "INTRADAY_OVERLAY_PUBLISHED",
            correlation_id="intraday-overlay",
            payload={"revision": payload["revision"]},
        )

    def _historical_bars(self, symbol: str, trading_date: str, *, days: int) -> list[KisMinuteBar]:
        root = self.store.root / symbol
        dates = sorted(
            path.stem
            for path in root.glob("*.json")
            if path.stem < trading_date and self.store.is_complete(symbol, path.stem)
        )[-days:]
        if len(dates) != days:
            raise ValueError(f"{symbol} does not have {days} complete historical days")
        return [bar for day in dates for bar in self.store.load(symbol, day)]


def _commit_public_overlay(repository: Path, payload: dict[str, object]) -> None:
    if not repository.exists():
        raise RuntimeError(f"dashboard repository does not exist: {repository}")
    if _git(repository, "status", "--porcelain").strip():
        raise RuntimeError("dashboard repository has uncommitted changes")
    target = repository / "intraday_overlay.json"
    _atomic_json(target, payload)
    _git(repository, "add", "--", target.name)
    staged = subprocess.run(
        ["git", "-C", str(repository), "diff", "--cached", "--quiet"],
        check=False,
    )
    if staged.returncode not in {0, 1}:
        raise RuntimeError("failed to inspect staged intraday overlay")
    if staged.returncode == 0:
        return
    _git(repository, "commit", "-m", f"Update intraday overlay {payload['revision']}")
    _git(repository, "push", "origin", "HEAD:main")


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout
