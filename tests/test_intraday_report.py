from __future__ import annotations

from datetime import date
from decimal import Decimal

from danta.adapters.kis.client import KisMinuteBar
from danta.adapters.krx.client import DailyBar, MarketDataset
from danta.services.intraday_report import aggregate_hour_bars, balanced_prefilter


def test_balanced_prefilter_applies_all_three_thresholds() -> None:
    dates = [date(2026, 7, day) for day in range(13, 20)]

    def bars(price: str, value: str) -> list[DailyBar]:
        return [
            DailyBar(
                trading_date=trading_date,
                close=Decimal(price),
                volume=Decimal("1000"),
                trading_value=Decimal(value),
            )
            for trading_date in dates
        ]

    dataset = MarketDataset(
        bars={
            "000001": bars("5000", "5000000000"),
            "000002": bars("4999", "9000000000"),
            "000003": bars("10000", "4999999999"),
            "000004": bars("10000", "9000000000"),
        },
        names={
            "000001": "통과",
            "000002": "저가",
            "000003": "저유동",
            "000004": "테스트우",
        },
        flows={},
        trading_dates=dates,
        market_caps={
            "000001": Decimal("500000000000"),
            "000002": Decimal("900000000000"),
            "000003": Decimal("900000000000"),
            "000004": Decimal("900000000000"),
        },
    )

    assert [item.symbol for item in balanced_prefilter(dataset)] == ["000001"]


def test_aggregate_hour_bars_uses_full_ohlcv() -> None:
    rows = [
        KisMinuteBar(
            trading_date="20260724",
            trading_time=trading_time,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            accumulated_trading_value=0,
        )
        for trading_time, open_, high, low, close, volume in [
            ("090000", 100, 105, 99, 103, 10),
            ("095900", 103, 110, 102, 108, 20),
            ("100000", 108, 109, 101, 102, 30),
        ]
    ]

    bars = aggregate_hour_bars(rows)

    assert len(bars) == 2
    assert bars[0].open == 100
    assert bars[0].high == 110
    assert bars[0].low == 99
    assert bars[0].close == 108
    assert bars[0].volume == 30
