from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from danta.adapters.kis.client import KisMinuteBar
from danta.services.candidate_report import CandidateReportError
from danta.services.intraday_report import MinuteBarStore, _analyze_symbol

KST = timezone(timedelta(hours=9))
HUNDRED = Decimal("100")
StrategyName = Literal["ACTIVE_BOX", "PERIOD_BOX"]
Outcome = Literal["TARGET", "STOP", "TIMEOUT"]


class WalkForwardTrade(BaseModel):
    strategy: StrategyName
    symbol: str = Field(pattern=r"^[0-9A-Z]{6}$")
    signal_date: str = Field(pattern=r"^\d{8}$")
    entry_price: Decimal = Field(gt=0)
    target_price: Decimal = Field(gt=0)
    stop_price: Decimal = Field(gt=0)
    outcome: Outcome
    exit_date: str = Field(pattern=r"^\d{8}$")
    holding_trading_days: int = Field(ge=1, le=5)
    holding_market_hours: Decimal = Field(gt=0)
    gross_return_pct: Decimal
    net_return_pct: Decimal
    mfe_pct: Decimal
    mae_pct: Decimal
    active_start_date: str | None = Field(default=None, pattern=r"^\d{8}$")
    active_confidence: Literal["HIGH", "MEDIUM", "LOW"] | None = None


class WalkForwardSummary(BaseModel):
    strategy: StrategyName
    trades: int = Field(ge=0)
    target_count: int = Field(ge=0)
    stop_count: int = Field(ge=0)
    timeout_count: int = Field(ge=0)
    target_rate_pct: Decimal = Field(ge=0, le=100)
    stop_rate_pct: Decimal = Field(ge=0, le=100)
    average_net_return_pct: Decimal
    median_net_return_pct: Decimal
    average_mfe_pct: Decimal
    average_mae_pct: Decimal
    profit_factor: Decimal | None = Field(default=None, ge=0)
    max_drawdown_pct: Decimal = Field(le=0)


class ActiveBoxWalkForwardReport(BaseModel):
    experiment_id: Literal["PI-014-WF-V1"] = "PI-014-WF-V1"
    generated_at: datetime
    data_root: str
    training_days: int = Field(ge=4, le=21)
    holding_days: int = Field(ge=1, le=5)
    round_trip_cost_bps: Decimal = Field(ge=0)
    flow_data_status: Literal["NOT_AVAILABLE_FOR_HISTORICAL_CUTOFF"]
    symbols_scanned: int = Field(ge=0)
    symbols_with_evaluable_history: int = Field(ge=0)
    cutoff_points_evaluated: int = Field(ge=0)
    sample_status: Literal["SUFFICIENT", "INSUFFICIENT_SAMPLE"]
    summaries: list[WalkForwardSummary]
    trades: list[WalkForwardTrade]


def _complete_dates(store: MinuteBarStore, symbol: str) -> list[str]:
    symbol_root = store.root / symbol
    if not symbol_root.exists():
        return []
    return sorted(
        path.stem
        for path in symbol_root.glob("*.json")
        if len(path.stem) == 8 and store.is_complete(symbol, path.stem)
    )


def _load_dates(
    store: MinuteBarStore,
    symbol: str,
    trading_dates: list[str],
) -> list[KisMinuteBar]:
    return sorted(
        [
            bar
            for trading_date in trading_dates
            for bar in store.load(symbol, trading_date)
        ],
        key=lambda item: (item.trading_date, item.trading_time),
    )


def _evaluate_trade(
    *,
    strategy: StrategyName,
    symbol: str,
    signal_date: str,
    entry_price: Decimal,
    target_price: Decimal,
    future_bars: list[KisMinuteBar],
    round_trip_cost_bps: Decimal,
    active_start_date: str | None = None,
    active_confidence: Literal["HIGH", "MEDIUM", "LOW"] | None = None,
) -> WalkForwardTrade:
    if not future_bars:
        raise ValueError("future bars are required")
    stop_price = entry_price * Decimal("0.93")
    dates = sorted({bar.trading_date for bar in future_bars})
    date_index = {trading_date: index + 1 for index, trading_date in enumerate(dates)}
    maximum_high = entry_price
    minimum_low = entry_price
    outcome: Outcome = "TIMEOUT"
    exit_bar = future_bars[-1]
    exit_price = Decimal(exit_bar.close)
    elapsed_bars = len(future_bars)

    for index, bar in enumerate(future_bars, start=1):
        high = Decimal(bar.high)
        low = Decimal(bar.low)
        maximum_high = max(maximum_high, high)
        minimum_low = min(minimum_low, low)
        if low <= stop_price:
            outcome = "STOP"
            exit_bar = bar
            exit_price = stop_price
            elapsed_bars = index
            break
        if high >= target_price:
            outcome = "TARGET"
            exit_bar = bar
            exit_price = target_price
            elapsed_bars = index
            break

    gross_return = (exit_price / entry_price - Decimal("1")) * HUNDRED
    cost_pct = round_trip_cost_bps / HUNDRED
    net_return = gross_return - cost_pct
    return WalkForwardTrade(
        strategy=strategy,
        symbol=symbol,
        signal_date=signal_date,
        entry_price=entry_price.quantize(Decimal("0.01")),
        target_price=target_price.quantize(Decimal("0.01")),
        stop_price=stop_price.quantize(Decimal("0.01")),
        outcome=outcome,
        exit_date=exit_bar.trading_date,
        holding_trading_days=date_index[exit_bar.trading_date],
        holding_market_hours=(
            Decimal(elapsed_bars) / Decimal("60")
        ).quantize(Decimal("0.01")),
        gross_return_pct=gross_return.quantize(Decimal("0.01")),
        net_return_pct=net_return.quantize(Decimal("0.01")),
        mfe_pct=(
            (maximum_high / entry_price - Decimal("1")) * HUNDRED
        ).quantize(Decimal("0.01")),
        mae_pct=(
            (minimum_low / entry_price - Decimal("1")) * HUNDRED
        ).quantize(Decimal("0.01")),
        active_start_date=active_start_date,
        active_confidence=active_confidence,
    )


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        return Decimal("0")
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _maximum_drawdown(returns: list[Decimal]) -> Decimal:
    equity = Decimal("1")
    peak = equity
    maximum_drawdown = Decimal("0")
    for value in returns:
        equity *= Decimal("1") + value / HUNDRED
        peak = max(peak, equity)
        drawdown = (equity / peak - Decimal("1")) * HUNDRED
        maximum_drawdown = min(maximum_drawdown, drawdown)
    return maximum_drawdown


def _summarize(
    strategy: StrategyName,
    trades: list[WalkForwardTrade],
) -> WalkForwardSummary:
    selected = [trade for trade in trades if trade.strategy == strategy]
    count = len(selected)
    target_count = sum(trade.outcome == "TARGET" for trade in selected)
    stop_count = sum(trade.outcome == "STOP" for trade in selected)
    timeout_count = sum(trade.outcome == "TIMEOUT" for trade in selected)
    returns = [trade.net_return_pct for trade in selected]
    positive = sum((value for value in returns if value > 0), Decimal("0"))
    negative = abs(sum((value for value in returns if value < 0), Decimal("0")))
    return WalkForwardSummary(
        strategy=strategy,
        trades=count,
        target_count=target_count,
        stop_count=stop_count,
        timeout_count=timeout_count,
        target_rate_pct=(
            Decimal(target_count) / Decimal(count) * HUNDRED
            if count
            else Decimal("0")
        ).quantize(Decimal("0.01")),
        stop_rate_pct=(
            Decimal(stop_count) / Decimal(count) * HUNDRED
            if count
            else Decimal("0")
        ).quantize(Decimal("0.01")),
        average_net_return_pct=(
            sum(returns, Decimal("0")) / Decimal(count)
            if count
            else Decimal("0")
        ).quantize(Decimal("0.01")),
        median_net_return_pct=_median(returns).quantize(Decimal("0.01")),
        average_mfe_pct=(
            sum((trade.mfe_pct for trade in selected), Decimal("0"))
            / Decimal(count)
            if count
            else Decimal("0")
        ).quantize(Decimal("0.01")),
        average_mae_pct=(
            sum((trade.mae_pct for trade in selected), Decimal("0"))
            / Decimal(count)
            if count
            else Decimal("0")
        ).quantize(Decimal("0.01")),
        profit_factor=(
            (positive / negative).quantize(Decimal("0.01"))
            if negative > 0
            else None
        ),
        max_drawdown_pct=_maximum_drawdown(returns).quantize(Decimal("0.01")),
    )


def run_active_box_walk_forward(
    store: MinuteBarStore,
    *,
    training_days: int = 7,
    holding_days: int = 5,
    round_trip_cost_bps: Decimal = Decimal("35"),
) -> ActiveBoxWalkForwardReport:
    if training_days < 4:
        raise ValueError("training_days must be at least 4")
    if not 1 <= holding_days <= 5:
        raise ValueError("holding_days must be between 1 and 5")
    symbols = sorted(
        path.name
        for path in store.root.iterdir()
        if path.is_dir() and len(path.name) == 6
    ) if store.root.exists() else []
    trades: list[WalkForwardTrade] = []
    evaluable_symbols = 0
    cutoff_points = 0

    for symbol in symbols:
        dates = _complete_dates(store, symbol)
        minimum_dates = training_days + holding_days
        if len(dates) < minimum_dates:
            continue
        evaluable_symbols += 1
        next_allowed: dict[StrategyName, int] = {
            "ACTIVE_BOX": training_days - 1,
            "PERIOD_BOX": training_days - 1,
        }
        for cutoff in range(training_days - 1, len(dates) - holding_days):
            cutoff_points += 1
            training_dates = dates[cutoff - training_days + 1 : cutoff + 1]
            future_dates = dates[cutoff + 1 : cutoff + 1 + holding_days]
            training_bars = _load_dates(store, symbol, training_dates)
            future_bars = _load_dates(store, symbol, future_dates)
            try:
                analysis = _analyze_symbol(symbol, training_bars)
            except CandidateReportError:
                continue
            if analysis.active is None:
                continue
            current = Decimal(training_bars[-1].close)
            active = analysis.active
            active_signal = (
                cutoff >= next_allowed["ACTIVE_BOX"]
                and active.lower_zone_low <= current <= active.lower_zone_high
                and current >= active.structural_invalidation_price
                and active.upper_zone_low > current
                and active.upper_reaches >= 1
            )
            if active_signal:
                trade = _evaluate_trade(
                    strategy="ACTIVE_BOX",
                    symbol=symbol,
                    signal_date=training_dates[-1],
                    entry_price=current,
                    target_price=active.upper_zone_low,
                    future_bars=future_bars,
                    round_trip_cost_bps=round_trip_cost_bps,
                    active_start_date=active.start_date,
                    active_confidence=active.confidence,
                )
                trades.append(trade)
                next_allowed["ACTIVE_BOX"] = cutoff + trade.holding_trading_days + 1

            period_width = analysis.high - analysis.low
            period_lower_high = analysis.low + period_width * Decimal("0.12")
            period_upper_low = analysis.high - period_width * Decimal("0.12")
            period_signal = (
                cutoff >= next_allowed["PERIOD_BOX"]
                and analysis.low <= current <= period_lower_high
                and current >= analysis.low * Decimal("0.98")
                and period_upper_low > current
                and analysis.target_reaches >= 1
            )
            if period_signal:
                trade = _evaluate_trade(
                    strategy="PERIOD_BOX",
                    symbol=symbol,
                    signal_date=training_dates[-1],
                    entry_price=current,
                    target_price=period_upper_low,
                    future_bars=future_bars,
                    round_trip_cost_bps=round_trip_cost_bps,
                )
                trades.append(trade)
                next_allowed["PERIOD_BOX"] = cutoff + trade.holding_trading_days + 1

    summaries = [
        _summarize("ACTIVE_BOX", trades),
        _summarize("PERIOD_BOX", trades),
    ]
    sample_status: Literal["SUFFICIENT", "INSUFFICIENT_SAMPLE"] = (
        "SUFFICIENT"
        if all(summary.trades >= 30 for summary in summaries)
        else "INSUFFICIENT_SAMPLE"
    )
    return ActiveBoxWalkForwardReport(
        generated_at=datetime.now(KST),
        data_root=str(Path(store.root)),
        training_days=training_days,
        holding_days=holding_days,
        round_trip_cost_bps=round_trip_cost_bps,
        flow_data_status="NOT_AVAILABLE_FOR_HISTORICAL_CUTOFF",
        symbols_scanned=len(symbols),
        symbols_with_evaluable_history=evaluable_symbols,
        cutoff_points_evaluated=cutoff_points,
        sample_status=sample_status,
        summaries=summaries,
        trades=trades,
    )
