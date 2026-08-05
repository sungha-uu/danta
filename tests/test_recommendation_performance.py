from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from danta.adapters.kis.client import KisMinuteBar
from danta.dashboard.demo import demo_report
from danta.services.intraday_report import MinuteBarStore
from danta.services.recommendation_performance import (
    RecommendationPerformanceError,
    RecommendationPerformanceSummary,
    RecommendationPerformanceTracker,
    RecommendationSnapshot,
)

KST = ZoneInfo("Asia/Seoul")


class CompleteTestMinuteBarStore(MinuteBarStore):
    def is_complete(self, symbol: str, trading_date: str) -> bool:
        return bool(self.load(symbol, trading_date))


def _dated_report(value: datetime):
    report = demo_report()
    return report.model_copy(update={"data_as_of": value})


def _save_future_path(
    store: MinuteBarStore,
    *,
    report,
    dates: list[str],
) -> None:
    for candidate in report.candidates:
        reference = int(candidate.current_price)
        recommended = candidate.windows["14"].ai_grade in {
            "STRONG_RECOMMEND",
            "RECOMMEND",
        }
        for trading_date in dates:
            if recommended:
                high = int(Decimal(reference) * Decimal("1.07"))
                low = int(Decimal(reference) * Decimal("0.99"))
                close = int(Decimal(reference) * Decimal("1.02"))
            else:
                high = int(Decimal(reference) * Decimal("1.01"))
                low = int(Decimal(reference) * Decimal("0.92"))
                close = int(Decimal(reference) * Decimal("0.98"))
            store.save(
                candidate.code,
                trading_date,
                [
                    KisMinuteBar(
                        trading_date=trading_date,
                        trading_time="090000",
                        open=reference,
                        high=high,
                        low=low,
                        close=close,
                        volume=100,
                        accumulated_trading_value=1000,
                    )
                ],
            )


def test_tracker_freezes_all_top_50_and_updates_future_outcomes(
    tmp_path: Path,
) -> None:
    tracker = RecommendationPerformanceTracker(
        tmp_path / "performance",
        minimum_samples_per_group=10,
    )
    store = CompleteTestMinuteBarStore(tmp_path / "bars")
    first_report = _dated_report(datetime(2026, 7, 24, 15, 30, tzinfo=KST))
    first = tracker.update(
        first_report,
        store,
        now=datetime(2026, 7, 24, 16, 0, tzinfo=KST),
    )
    assert first.snapshot_count == 1
    assert first.frozen_observation_count == 50
    assert first.completed_outcome_count == 0
    _save_future_path(
        store,
        report=first_report,
        dates=["20260727", "20260728", "20260729", "20260730", "20260731"],
    )

    second_report = _dated_report(datetime(2026, 7, 31, 15, 30, tzinfo=KST))
    second = tracker.update(
        second_report,
        store,
        now=datetime(2026, 7, 31, 16, 0, tzinfo=KST),
    )
    assert second.snapshot_count == 2
    assert second.frozen_observation_count == 100
    assert second.completed_outcome_count == 150
    assert second.recommendation_edge_status == "EDGE_OBSERVED"

    snapshot = RecommendationSnapshot.model_validate_json(
        first.snapshot_path.read_text(encoding="utf-8")
    )
    recommended = next(
        item
        for item in snapshot.observations
        if item.grade == "STRONG_RECOMMEND"
    )
    not_recommended = next(
        item
        for item in snapshot.observations
        if item.grade == "NOT_RECOMMEND"
    )
    assert recommended.outcomes["5"].first_hits["plus_6"] is not None
    assert recommended.outcomes["5"].plus_6_before_minus_7 is True
    assert not_recommended.outcomes["5"].first_hits["minus_7"] is not None
    assert not_recommended.outcomes["5"].plus_6_before_minus_7 is False

    summary = RecommendationPerformanceSummary.model_validate_json(
        second.summary_path.read_text(encoding="utf-8")
    )
    recommended_group = next(
        item
        for item in summary.groups
        if item.group == "RECOMMENDED" and item.horizon_trading_days == 5
    )
    baseline_group = next(
        item
        for item in summary.groups
        if item.group == "NOT_RECOMMENDED" and item.horizon_trading_days == 5
    )
    assert recommended_group.plus_6_hit_rate_pct == Decimal("100.00")
    assert baseline_group.plus_6_hit_rate_pct == Decimal("0.00")
    assert summary.missed_plus_6_count_1d == 0


def test_summary_records_not_recommended_next_day_winner(tmp_path: Path) -> None:
    tracker = RecommendationPerformanceTracker(tmp_path / "performance")
    store = CompleteTestMinuteBarStore(tmp_path / "bars")
    report = _dated_report(datetime(2026, 7, 24, 15, 30, tzinfo=KST))
    tracker.update(report, store)
    missed = next(
        item
        for item in report.candidates
        if item.windows["14"].ai_grade == "NOT_RECOMMEND"
    )
    reference = int(missed.current_price)
    store.save(
        missed.code,
        "20260727",
        [
            KisMinuteBar(
                trading_date="20260727",
                trading_time="090000",
                open=reference,
                high=int(Decimal(reference) * Decimal("1.08")),
                low=int(Decimal(reference) * Decimal("0.99")),
                close=int(Decimal(reference) * Decimal("1.07")),
                volume=100,
                accumulated_trading_value=1000,
            )
        ],
    )
    result = tracker.update(
        _dated_report(datetime(2026, 7, 27, 15, 30, tzinfo=KST)),
        store,
    )
    summary = RecommendationPerformanceSummary.model_validate_json(
        result.summary_path.read_text(encoding="utf-8")
    )

    assert summary.missed_plus_6_count_1d == 1
    assert summary.top_missed_winners_1d[0].symbol == missed.code
    assert summary.top_missed_winners_1d[0].mfe_pct >= Decimal("6")


def test_default_policy_stays_insufficient_until_both_groups_have_30_samples(
    tmp_path: Path,
) -> None:
    tracker = RecommendationPerformanceTracker(tmp_path / "performance")
    store = CompleteTestMinuteBarStore(tmp_path / "bars")
    first_report = _dated_report(datetime(2026, 7, 24, 15, 30, tzinfo=KST))
    tracker.update(first_report, store)
    _save_future_path(
        store,
        report=first_report,
        dates=["20260727", "20260728", "20260729", "20260730", "20260731"],
    )
    result = tracker.update(
        _dated_report(datetime(2026, 7, 31, 15, 30, tzinfo=KST)),
        store,
    )
    assert result.recommendation_edge_status == "INSUFFICIENT_SAMPLE"


def test_tracker_refuses_to_rewrite_a_frozen_decision(
    tmp_path: Path,
) -> None:
    tracker = RecommendationPerformanceTracker(tmp_path / "performance")
    store = CompleteTestMinuteBarStore(tmp_path / "bars")
    report = _dated_report(datetime(2026, 7, 24, 15, 30, tzinfo=KST))
    tracker.update(report, store)
    changed = report.model_copy(deep=True)
    changed.candidates[0].windows["14"].ai_grade = "NOT_RECOMMEND"

    with pytest.raises(
        RecommendationPerformanceError,
        match="cannot be overwritten",
    ):
        tracker.update(changed, store)
