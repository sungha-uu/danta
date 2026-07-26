from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from danta.adapters.kis.client import KisMinuteBar
from danta.adapters.krx.client import DailyBar, MarketDataset
from danta.services.intraday_report import (
    HourBar,
    PrefilterCandidate,
    _Analyzed,
    _daily_dynamics,
    _entry_location_factor,
    _intraday_period_return,
    _score_all,
    _setup_eligible,
    _setup_grade,
    _setup_rejection_reasons,
    _target_reach_episodes,
    aggregate_hour_bars,
    balanced_prefilter,
)


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


def test_target_reach_requires_a_later_minute_after_lower_contact() -> None:
    rows = [
        KisMinuteBar(
            trading_date="20260724",
            trading_time=trading_time,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=10,
            accumulated_trading_value=0,
        )
        for trading_time, open_, high, low, close in [
            ("090000", 100, 111, 99, 108),
            ("090100", 108, 109, 105, 106),
            ("090200", 106, 110, 106, 110),
            ("090300", 110, 111, 100, 101),
        ]
    ]

    assert _target_reach_episodes(rows, Decimal("100")) == (2, 1, 1)


def test_target_reach_expires_after_three_trading_days() -> None:
    rows = [
        KisMinuteBar(
            trading_date=trading_date,
            trading_time="090000",
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=10,
            accumulated_trading_value=0,
        )
        for trading_date, open_, high, low, close in [
            ("20260720", 100, 101, 99, 100),
            ("20260721", 100, 105, 100, 104),
            ("20260722", 104, 109, 103, 108),
            ("20260723", 108, 111, 108, 110),
        ]
    ]

    assert _target_reach_episodes(rows, Decimal("100")) == (1, 0, 0)


def test_daily_dynamics_only_counts_rebound_after_the_daily_low() -> None:
    rows = [
        KisMinuteBar(
            trading_date=trading_date,
            trading_time=trading_time,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=10,
            accumulated_trading_value=0,
        )
        for trading_date, trading_time, open_, high, low, close in [
            ("20260723", "090000", 100, 115, 100, 114),
            ("20260723", "100000", 114, 114, 95, 96),
            ("20260723", "110000", 96, 100, 96, 99),
            ("20260724", "090000", 100, 102, 90, 91),
            ("20260724", "100000", 91, 104, 91, 103),
        ]
    ]

    metrics = _daily_dynamics(rows, Decimal("103"))

    assert metrics[4:7] == (2, 1, 1)
    assert metrics[3] > Decimal("15")
    assert metrics[8] < 0


def test_scoring_preserves_typed_hour_bars_for_dashboard_modal() -> None:
    hour_bar = HourBar(
        trading_date="20260724",
        bucket="09",
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("99"),
        close=Decimal("108"),
        volume=Decimal("1000"),
    )
    analysis = _Analyzed(
        symbol="000001",
        low=Decimal("100"),
        high=Decimal("120"),
        amplitude=Decimal("18"),
        position=Decimal("20"),
        target_price=Decimal("110"),
        lower_contacts=1,
        target_reaches=1,
        target_pending=0,
        median_daily_range=Decimal("6"),
        max_daily_range=Decimal("12"),
        median_daily_rebound=Decimal("5"),
        max_daily_rebound=Decimal("10"),
        reach_days_5=4,
        reach_days_10=2,
        reach_days_15=0,
        current_to_window_high=Decimal("11"),
        lower_trend=Decimal("-2"),
        box_inclusion=Decimal("80"),
        hour_bars=[hour_bar],
        hourly_closes=[Decimal("108")],
        score=Decimal("0"),
    )
    candidate = PrefilterCandidate(
        symbol="000001",
        name="테스트",
        market_cap=Decimal("500000000000"),
        latest_price=Decimal("108"),
        average_trading_value=Decimal("5000000000"),
    )

    scored = _score_all([analysis], {"000001": candidate})

    assert scored[0].hour_bars == [hour_bar]
    assert isinstance(scored[0].hour_bars[0], HourBar)
    assert _setup_eligible(analysis)
    assert not _setup_eligible(replace(analysis, position=Decimal("68")))
    assert _setup_eligible(replace(analysis, lower_trend=Decimal("-20")))
    assert not _setup_eligible(replace(analysis, target_reaches=0))
    assert _setup_rejection_reasons(analysis) == ()
    assert _setup_rejection_reasons(
        replace(analysis, position=Decimal("68"), target_reaches=0)
    ) == (
        "현재 위치가 박스 하단 35% 밖",
        "하단 접촉 후 3거래일 내 +10% 도달 이력 없음",
    )


def test_entry_location_is_a_gate_not_a_small_bonus() -> None:
    assert _entry_location_factor(Decimal("20")) == Decimal("1.00")
    assert _entry_location_factor(Decimal("35")) == Decimal("0.90")
    assert _entry_location_factor(Decimal("68")) == Decimal("0.30")
    assert _entry_location_factor(Decimal("80")) == Decimal("0.10")
    assert _setup_grade(
        Decimal("90"), Decimal("68"), Decimal("5")
    ) == "NOT_RECOMMEND"
    assert _setup_grade(
        Decimal("90"), Decimal("20"), Decimal("-9")
    ) == "STRONG_RECOMMEND"


def test_ready_period_return_matches_chart_endpoints() -> None:
    result = _intraday_period_return(
        Decimal("482000"),
        [Decimal("478500"), Decimal("489500"), Decimal("482000")],
    )

    assert result == Decimal("0.73")
