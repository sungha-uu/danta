from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

from danta.adapters.krx.client import DailyBar, MarketDataset
from danta.services.candidate_report import build_quant_report


def _dataset() -> MarketDataset:
    start = date(2026, 5, 25)
    dates = [start + timedelta(days=index) for index in range(42)]
    bars: dict[str, list[DailyBar]] = {}
    names: dict[str, str] = {}
    flows: dict[int, dict[str, dict[str, Decimal]]] = {
        7: {},
        14: {},
        21: {},
    }
    for stock_index in range(40):
        symbol = f"{stock_index + 1:06d}"
        names[symbol] = f"테스트{stock_index + 1}"
        series: list[DailyBar] = []
        for day_index, trading_date in enumerate(dates):
            wave = math.sin((day_index + stock_index) * 0.9) * (0.06 + stock_index / 1000)
            close = Decimal(str(round(20_000 * (1 + wave), 2)))
            series.append(
                DailyBar(
                    trading_date=trading_date,
                    close=close,
                    volume=Decimal(1_000_000 + stock_index * 10_000),
                    trading_value=Decimal(30_000_000_000 + stock_index * 1_000_000_000),
                )
            )
        bars[symbol] = series
        for days in (7, 14, 21):
            flows[days][symbol] = {
                "retail": Decimal("-30"),
                "foreign": Decimal("20"),
                "institution": Decimal("10"),
                "financial_investment": Decimal("5"),
                "pension": Decimal("2"),
            }
    return MarketDataset(bars=bars, names=names, flows=flows, trading_dates=dates)


def test_quant_report_contains_ranked_real_candidate_shape() -> None:
    report = build_quant_report(_dataset())

    assert report.is_demo is False
    assert report.calculation_version == "box-quant-v1"
    assert report.strategy_status == "RESEARCH_ONLY"
    assert report.analysis_bar_interval_minutes is None
    assert report.model_id == "quant-baseline-no-llm"
    assert len(report.candidates) == 30
    for window in ("7", "14", "21"):
        assert sorted(candidate.windows[window].rank for candidate in report.candidates) == list(
            range(1, 31)
        )


def test_round_trip_count_changes_with_window() -> None:
    report = build_quant_report(_dataset())
    candidate = report.candidates[0]

    assert candidate.windows["21"].traversal_count >= candidate.windows["7"].traversal_count
