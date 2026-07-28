from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from danta.adapters.kis.realtime import OrderBookTick, RealtimeEvent, TradeTick
from danta.domain.market import MarketRisk, MarketSnapshot


def _clip(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), value))


@dataclass(frozen=True, slots=True)
class SignalPolicy:
    version: str = "market-signal-v1"
    maximum_trade_ticks: int = 120

    def __post_init__(self) -> None:
        if self.maximum_trade_ticks < 10:
            raise ValueError("maximum_trade_ticks must be at least 10")


class RollingMarketSignal:
    """Transforms official KIS trade/orderbook fields into normalized local signals."""

    def __init__(self, symbol: str, *, policy: SignalPolicy | None = None) -> None:
        self.symbol = symbol
        self.policy = policy or SignalPolicy()
        self._trades: deque[TradeTick] = deque(maxlen=self.policy.maximum_trade_ticks)
        self._orderbook: OrderBookTick | None = None

    def update(self, event: RealtimeEvent) -> None:
        if event.symbol != self.symbol:
            raise ValueError("event symbol does not match signal state")
        if isinstance(event, TradeTick):
            self._trades.append(event)
        elif isinstance(event, OrderBookTick):
            self._orderbook = event

    @property
    def trade_count(self) -> int:
        return len(self._trades)

    @property
    def ready(self) -> bool:
        return bool(self._trades)

    def snapshot(
        self,
        *,
        now: datetime,
        market_risk: MarketRisk,
        market_stress_score: Decimal,
        box_valid: bool,
        data_fresh: bool,
    ) -> MarketSnapshot:
        if not self._trades:
            raise ValueError("trade data is not available")
        latest = self._trades[-1]
        orderbook = self._orderbook
        best_ask = orderbook.best_ask if orderbook is not None else latest.best_ask
        best_bid = orderbook.best_bid if orderbook is not None else latest.best_bid

        count_total = latest.sell_trade_count + latest.buy_trade_count
        sell_count_ratio = (
            Decimal(latest.sell_trade_count) / Decimal(count_total)
            if count_total > 0
            else Decimal("0.5")
        )
        strength_sell = _clip(
            (Decimal("100") - latest.trade_strength) / Decimal("100")
        )
        total_ask = (
            orderbook.total_ask_quantity
            if orderbook is not None
            else latest.total_ask_quantity
        )
        total_bid = (
            orderbook.total_bid_quantity
            if orderbook is not None
            else latest.total_bid_quantity
        )
        depth_total = total_ask + total_bid
        ask_depth_ratio = (
            Decimal(total_ask) / Decimal(depth_total)
            if depth_total > 0
            else Decimal("0.5")
        )
        prices = [tick.price for tick in self._trades]
        older = prices[max(0, len(prices) - min(20, len(prices)))]
        price_decline = _clip(
            Decimal(max(0, older - latest.price)) / Decimal(max(1, older)) * Decimal("20")
        )
        sell_pressure = _clip(
            (
                sell_count_ratio
                + strength_sell
                + ask_depth_ratio
                + price_decline
            )
            / Decimal("4")
        )

        recent_prices = prices[-min(30, len(prices)) :]
        low = min(recent_prices)
        high = max(recent_prices)
        range_size = max(1, high - low)
        range_position = Decimal(latest.price - low) / Decimal(range_size)
        new_low_penalty = Decimal("0") if latest.price == low else Decimal("1")
        stabilization = _clip((new_low_penalty + range_position) / Decimal("2"))

        bid_depth_ratio = Decimal("1") - ask_depth_ratio
        strength_buy = _clip(latest.trade_strength / Decimal("150"))
        buy_recovery = _clip(
            (bid_depth_ratio + strength_buy + range_position) / Decimal("3")
        )
        weakness = _clip((sell_pressure + (Decimal("1") - range_position)) / Decimal("2"))

        return MarketSnapshot(
            symbol=self.symbol,
            observed_at=latest.observed_at,
            last_price=latest.price,
            best_bid=best_bid,
            best_ask=best_ask,
            sell_pressure_score=sell_pressure,
            stabilization_score=stabilization,
            buy_recovery_score=buy_recovery,
            weakness_score=weakness,
            market_stress_score=_clip(market_stress_score),
            market_risk=market_risk,
            box_valid=box_valid,
            data_fresh=data_fresh and latest.observed_at <= now,
        )
