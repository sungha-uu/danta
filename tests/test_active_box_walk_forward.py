from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from danta.adapters.kis.client import KisMinuteBar
from danta.services.active_box_walk_forward import (
    _evaluate_trade,
    run_active_box_walk_forward,
)
from danta.services.intraday_report import MinuteBarStore


def _minute_bar(
    trading_date: str,
    trading_time: str,
    *,
    price: int,
    low: int | None = None,
    high: int | None = None,
) -> KisMinuteBar:
    return KisMinuteBar(
        trading_date=trading_date,
        trading_time=trading_time,
        open=price,
        high=high if high is not None else price + 1,
        low=low if low is not None else price - 1,
        close=price,
        volume=100,
        accumulated_trading_value=0,
    )


def _save_day(
    store: MinuteBarStore,
    symbol: str,
    trading_date: str,
    price: int,
) -> None:
    bars: list[KisMinuteBar] = []
    for index in range(179):
        hour = 9 + index // 60
        minute = index % 60
        bars.append(
            _minute_bar(
                trading_date,
                f"{hour:02d}{minute:02d}00",
                price=price,
            )
        )
    bars.append(_minute_bar(trading_date, "152000", price=price))
    store.save(symbol, trading_date, bars)


def test_same_minute_target_and_stop_is_counted_as_stop() -> None:
    future = [
        _minute_bar(
            "20260724",
            "090000",
            price=100,
            low=92,
            high=112,
        )
    ]

    trade = _evaluate_trade(
        strategy="ACTIVE_BOX",
        symbol="000001",
        signal_date="20260723",
        entry_price=Decimal("100"),
        target_price=Decimal("110"),
        future_bars=future,
        round_trip_cost_bps=Decimal("35"),
    )

    assert trade.outcome == "STOP"
    assert trade.gross_return_pct == Decimal("-7.00")
    assert trade.net_return_pct == Decimal("-7.35")


def test_walk_forward_freezes_training_box_before_future_evaluation(
    tmp_path: Path,
) -> None:
    store = MinuteBarStore(tmp_path / "1m")
    start = date(2026, 7, 1)
    prices = [100, 110, 100, 110, 100, 110, 100, 105, 112, 110, 108, 107]
    for offset, price in enumerate(prices):
        _save_day(
            store,
            "000001",
            (start + timedelta(days=offset)).strftime("%Y%m%d"),
            price,
        )

    report = run_active_box_walk_forward(store)

    assert report.symbols_with_evaluable_history == 1
    assert report.cutoff_points_evaluated == 1
    assert report.flow_data_status == "NOT_AVAILABLE_FOR_HISTORICAL_CUTOFF"
    active = next(
        trade for trade in report.trades if trade.strategy == "ACTIVE_BOX"
    )
    assert active.signal_date == "20260707"
    assert active.exit_date > active.signal_date
    assert active.outcome == "TARGET"
    assert active.target_price > active.entry_price
