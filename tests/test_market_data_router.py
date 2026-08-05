from datetime import UTC, datetime
from decimal import Decimal

from danta.adapters.kis.realtime import TradeTick
from danta.services.market_data_router import MarketDataRouter


class FakeCore:
    def __init__(self) -> None:
        self.symbols: list[str] = []

    async def process_event(self, event: TradeTick) -> None:
        self.symbols.append(event.symbol)


def tick(symbol: str) -> TradeTick:
    return TradeTick(
        symbol=symbol,
        observed_at=datetime.now(UTC),
        price=100,
        best_ask=101,
        best_bid=99,
        trade_volume=1,
        accumulated_value=100,
        sell_trade_count=1,
        buy_trade_count=1,
        trade_strength=Decimal("100"),
        ask_quantity=1,
        bid_quantity=1,
        total_ask_quantity=1,
        total_bid_quantity=1,
    )


async def test_router_serializes_each_symbol_independently() -> None:
    core = FakeCore()
    router = MarketDataRouter(core)  # type: ignore[arg-type]
    router.start(["005930", "000660"])
    assert await router.route(tick("005930"))
    assert await router.route(tick("000660"))
    await router.queues["005930"].join()
    await router.queues["000660"].join()
    await router.stop()
    assert sorted(core.symbols) == ["000660", "005930"]


async def test_router_releases_terminal_symbol_worker() -> None:
    router = MarketDataRouter(FakeCore())  # type: ignore[arg-type]
    router.start(["005930", "000660"])

    await router.stop_symbols(["005930"])

    assert "005930" not in router.queues
    assert "005930" not in router.tasks
    assert "000660" in router.tasks
    await router.stop()
