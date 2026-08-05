from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from danta.adapters.kis.realtime import ExpectedPriceTick, MarketVenue, TradeTick
from danta.domain.premarket import PremarketPolicy
from danta.services.unified_market_monitor import UnifiedTradingMonitor

KST = ZoneInfo("Asia/Seoul")


def _trade(venue: MarketVenue) -> TradeTick:
    return TradeTick(
        symbol="000660",
        observed_at=datetime(2026, 7, 30, 18, 0, tzinfo=KST),
        price=1_320_000,
        best_ask=1_321_000,
        best_bid=1_320_000,
        trade_volume=10,
        accumulated_value=1_000_000,
        sell_trade_count=60,
        buy_trade_count=40,
        trade_strength=Decimal("80"),
        ask_quantity=100,
        bid_quantity=100,
        total_ask_quantity=1000,
        total_bid_quantity=1000,
        venue=venue,
    )


class _Realtime:
    def __init__(self, event: TradeTick) -> None:
        self.event = event
        self.venues: list[MarketVenue] = []

    async def stream(self, symbols: list[str], *, venue: MarketVenue) -> AsyncIterator[TradeTick]:
        assert symbols == ["000660"]
        self.venues.append(venue)
        yield self.event

    async def stream_premarket(
        self,
        symbols: list[str],
    ) -> AsyncIterator[TradeTick | ExpectedPriceTick]:
        assert symbols == ["000660"]
        self.venues.append(MarketVenue.NXT)
        yield self.event
        yield ExpectedPriceTick(
            symbol="000660",
            observed_at=self.event.observed_at,
            expected_price=1_319_000,
            best_ask=1_320_000,
            best_bid=1_319_000,
            expected_volume=100,
            change_rate=Decimal("-1.2"),
            venue=MarketVenue.KRX,
        )


class _Router:
    def __init__(self) -> None:
        self.queues = {"000660": object()}
        self.events: list[TradeTick] = []

    async def route(self, event: TradeTick) -> bool:
        self.events.append(event)
        return True


class _Coordinator:
    def __init__(self) -> None:
        self.events: list[TradeTick] = []

    def process_event(self, event: TradeTick) -> None:
        self.events.append(event)


def _policy() -> PremarketPolicy:
    return PremarketPolicy(
        version="premarket-test-v1",
        approved=True,
        minimum_nxt_trade_samples=3,
        maximum_snapshot_age_seconds=30,
        early_loss_pct=Decimal("-3"),
        strong_loss_pct=Decimal("-5"),
        sell_pressure_threshold=Decimal("0.7"),
        market_stress_threshold=Decimal("0.75"),
    )


def _monitor(event: TradeTick) -> tuple[UnifiedTradingMonitor, _Router, _Coordinator]:
    router = _Router()
    coordinator = _Coordinator()
    monitor = UnifiedTradingMonitor(
        realtime=_Realtime(event),  # type: ignore[arg-type]
        router=router,  # type: ignore[arg-type]
        core=SimpleNamespace(),  # type: ignore[arg-type]
        premarket_policy=_policy(),
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        correlation_id="test",
        opening_reconcile=lambda: None,  # type: ignore[arg-type]
    )
    monitor.coordinator = coordinator  # type: ignore[assignment]
    return monitor, router, coordinator


async def test_nxt_event_is_observed_but_never_routed_to_order_core() -> None:
    monitor, router, coordinator = _monitor(_trade(MarketVenue.NXT))

    await monitor._consume_nxt()

    assert router.events == []
    assert len(coordinator.events) == 1


async def test_krx_event_is_routed_to_the_trading_core() -> None:
    monitor, router, coordinator = _monitor(_trade(MarketVenue.KRX))

    await monitor._consume_krx()

    assert len(router.events) == 1
    assert coordinator.events == []


async def test_nxt_and_expected_prices_share_one_premarket_stream() -> None:
    monitor, router, coordinator = _monitor(_trade(MarketVenue.NXT))

    await monitor._consume_nxt_and_expected()

    assert router.events == []
    assert len(coordinator.events) == 2
