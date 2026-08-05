from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from danta.adapters.kis.client import KisMinuteBar
from danta.adapters.krx.client import DailyBar, MarketDataset
from danta.services.intraday_report import (
    HourBar,
    MinuteBarStore,
    PrefilterCandidate,
    _active_episode_stats,
    _active_regime_start,
    _ActiveBox,
    _Analyzed,
    _daily_dynamics,
    _decline_shape,
    _entry_location_factor,
    _intraday_period_return,
    _repeated_up_swings,
    _score_all,
    _setup_eligible,
    _setup_grade,
    _setup_rejection_reasons,
    _target_reach_episodes,
    aggregate_hour_bars,
    balanced_prefilter,
    market_cap_top_universe,
)


def test_repeated_up_swings_count_non_overlapping_six_percent_legs() -> None:
    closes = [100, 107, 110, 103, 96, 102, 108, 101]
    rows = [
        KisMinuteBar(
            trading_date="20260724",
            trading_time=f"09{index:02d}00",
            open=close,
            high=close,
            low=close,
            close=close,
            volume=10,
            accumulated_trading_value=1000,
        )
        for index, close in enumerate(closes)
    ]

    swings = _repeated_up_swings(rows)

    assert len(swings) == 2
    assert [item.status for item in swings] == ["CONFIRMED", "CONFIRMED"]
    assert swings[0].amplitude_pct == Decimal("10.0")
    assert swings[1].amplitude_pct == Decimal("12.500")
    assert [item.minutes_to_6pct for item in swings] == [1, 1]


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


def test_market_cap_top_universe_sorts_common_stocks_by_market_cap() -> None:
    dates = [date(2026, 7, 24)]
    dataset = MarketDataset(
        bars={
            "000001": [
                DailyBar(dates[0], Decimal("10000"), Decimal("100"), Decimal("1000"))
            ],
            "000002": [
                DailyBar(dates[0], Decimal("20000"), Decimal("100"), Decimal("2000"))
            ],
            "000003": [
                DailyBar(dates[0], Decimal("30000"), Decimal("100"), Decimal("3000"))
            ],
        },
        names={"000001": "중형", "000002": "대형", "000003": "대형우"},
        flows={},
        trading_dates=dates,
        market_caps={
            "000001": Decimal("100"),
            "000002": Decimal("300"),
            "000003": Decimal("500"),
        },
    )

    universe = market_cap_top_universe(dataset, limit=2)

    assert [item.symbol for item in universe] == ["000002", "000001"]

def test_market_cap_top_universe_excludes_latest_suspended_security() -> None:
    dates = [date(2026, 7, 30)]
    dataset = MarketDataset(
        bars={
            "000001": [
                DailyBar(dates[0], Decimal("10000"), Decimal("100"), Decimal("1000"))
            ],
            "000002": [
                DailyBar(dates[0], Decimal("20000"), Decimal("0"), Decimal("0"))
            ],
        },
        names={"000001": "거래중", "000002": "거래정지"},
        flows={},
        trading_dates=dates,
        market_caps={"000001": Decimal("100"), "000002": Decimal("300")},
    )

    universe = market_cap_top_universe(dataset, limit=1)

    assert [item.symbol for item in universe] == ["000001"]


def test_minute_store_accepts_complete_continuous_session_without_auction_bar(
    tmp_path,
) -> None:
    start = datetime(2026, 7, 24, 9, 0)
    rows = [
        KisMinuteBar(
            trading_date="20260724",
            trading_time=(start + timedelta(minutes=index)).strftime("%H%M%S"),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=10,
            accumulated_trading_value=1000,
        )
        for index in range(380)
    ]
    store = MinuteBarStore(tmp_path)
    store.save("005930", "20260724", rows)

    assert rows[-1].trading_time == "151900"
    assert store.is_complete("005930", "20260724")

    sparse_rows = rows[::2] + [
        replace(rows[-1], trading_time="153000"),
    ]
    store.save("000500", "20260724", sparse_rows)

    assert len(sparse_rows) == 191
    assert store.is_complete("000500", "20260724")

    quiet_close_rows = rows[:-1]
    store.save("012630", "20260724", quiet_close_rows)
    assert quiet_close_rows[-1].trading_time == "151800"
    assert store.is_complete("012630", "20260724")


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


def test_decline_shape_separates_good_pullback_from_structural_decline() -> None:
    assert _decline_shape(
        position=Decimal("20"),
        lower_trend=Decimal("-6"),
        upper_trend=Decimal("-2"),
        range_retention=Decimal("90"),
        rebound_retention=Decimal("85"),
    ) == "GOOD_PULLBACK"
    assert _decline_shape(
        position=Decimal("20"),
        lower_trend=Decimal("-10"),
        upper_trend=Decimal("-12"),
        range_retention=Decimal("70"),
        rebound_retention=Decimal("60"),
    ) == "STRUCTURAL_DECLINE"


def test_active_regime_starts_after_sustained_level_shift() -> None:
    bars = [
        HourBar(
            trading_date=f"202607{day:02d}",
            bucket="09",
            open=price,
            high=price + Decimal("1"),
            low=price - Decimal("1"),
            close=price,
            volume=Decimal("1000"),
        )
        for day, price in [
            (13, Decimal("100")),
            (14, Decimal("101")),
            (15, Decimal("85")),
            (16, Decimal("86")),
            (17, Decimal("84")),
            (18, Decimal("87")),
        ]
    ]

    assert _active_regime_start(bars) == "20260715"


def test_active_episode_treats_same_bar_target_and_stop_as_stop_first() -> None:
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
            ("090000", 100, 101, 99, 100),
            ("090100", 100, 112, 92, 105),
        ]
    ]

    stats = _active_episode_stats(
        rows,
        lower_zone_high=Decimal("100"),
        upper_zone_low=Decimal("110"),
        box_width=Decimal("10"),
    )

    assert stats[:5] == (1, 0, 1, 0, 0)
    assert stats[7] == Decimal("100")


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
        upper_trend=Decimal("-1"),
        range_retention=Decimal("90"),
        rebound_retention=Decimal("90"),
        decline_shape="GOOD_PULLBACK",
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
    assert not _setup_eligible(
        replace(analysis, decline_shape="STRUCTURAL_DECLINE")
    )
    assert not _setup_eligible(replace(analysis, target_reaches=0))
    assert _setup_rejection_reasons(analysis) == ()
    assert _setup_rejection_reasons(
        replace(analysis, position=Decimal("68"), target_reaches=0)
    ) == (
        "현재 위치가 선택 기간 박스 하단 35% 밖",
        "박스 하단 접촉 후 3거래일 내 실제 +10% 도달 이력 없음",
    )

    more_repeats = replace(
        analysis,
        symbol="000002",
        up_swing_count=3,
        average_up_swing=Decimal("7"),
        average_time_to_6pct_hours=Decimal("5"),
    )
    larger_but_fewer = replace(
        analysis,
        up_swing_count=2,
        average_up_swing=Decimal("20"),
        average_time_to_6pct_hours=Decimal("1"),
    )
    second_candidate = replace(candidate, symbol="000002")
    repeat_ranked = _score_all(
        [larger_but_fewer, more_repeats],
        {"000001": candidate, "000002": second_candidate},
    )
    assert [item.symbol for item in repeat_ranked] == ["000002", "000001"]

    active_reached_but_no_ten_pct = replace(
        analysis,
        active=_ActiveBox(
            start_date="20260720",
            trading_days=5,
            lower_zone_low=Decimal("100"),
            lower_zone_high=Decimal("102"),
            upper_zone_low=Decimal("106"),
            upper_zone_high=Decimal("108"),
            position=Decimal("20"),
            amplitude=Decimal("8"),
            upside_to_upper=Decimal("5"),
            inclusion=Decimal("80"),
            lower_contacts=1,
            upper_reaches=1,
            stop_first=0,
            timeouts=0,
            pending=0,
            completed_cycles=0,
            success_rate=Decimal("100"),
            stop_first_rate=Decimal("0"),
            median_time_to_target_hours=Decimal("2"),
            rebound_trend="표본 부족",
            confidence="LOW",
            structural_invalidation_price=Decimal("98"),
        ),
        target_reaches=0,
    )
    assert not _setup_eligible(active_reached_but_no_ten_pct)

    invalid_active = _ActiveBox(
        start_date="20260720",
        trading_days=5,
        lower_zone_low=Decimal("110"),
        lower_zone_high=Decimal("112"),
        upper_zone_low=Decimal("120"),
        upper_zone_high=Decimal("122"),
        position=Decimal("-20"),
        amplitude=Decimal("10"),
        upside_to_upper=Decimal("11"),
        inclusion=Decimal("80"),
        lower_contacts=1,
        upper_reaches=1,
        stop_first=0,
        timeouts=0,
        pending=0,
        completed_cycles=0,
        success_rate=Decimal("100"),
        stop_first_rate=Decimal("0"),
        median_time_to_target_hours=Decimal("2"),
        rebound_trend="표본 부족",
        confidence="LOW",
        structural_invalidation_price=Decimal("109"),
    )
    invalidated = replace(analysis, active=invalid_active)
    assert _setup_eligible(invalidated)
    assert _setup_rejection_reasons(invalidated) == ()

    target_already_passed = replace(analysis, target_price=Decimal("107"))
    assert not _setup_eligible(target_already_passed)
    assert "하단 기준 +10% 목표가를 현재가가 이미 통과" in (
        _setup_rejection_reasons(target_already_passed)
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
    assert _setup_grade(
        Decimal("90"), Decimal("20"), Decimal("-9"), target_reaches=0
    ) == "NOT_RECOMMEND"


def test_ready_period_return_matches_chart_endpoints() -> None:
    result = _intraday_period_return(
        Decimal("482000"),
        [Decimal("478500"), Decimal("489500"), Decimal("482000")],
    )

    assert result == Decimal("0.73")
