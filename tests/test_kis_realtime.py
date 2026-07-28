from datetime import UTC, datetime
from decimal import Decimal

from danta.adapters.kis.realtime import (
    EXPECTED_TRADE_COLUMNS,
    EXPECTED_TRADE_TR_ID,
    NXT_ORDERBOOK_COLUMNS,
    NXT_ORDERBOOK_TR_ID,
    NXT_TRADE_TR_ID,
    ORDERBOOK_COLUMNS,
    ORDERBOOK_TR_ID,
    TRADE_COLUMNS,
    TRADE_TR_ID,
    ExpectedPriceTick,
    MarketVenue,
    OrderBookTick,
    TradeTick,
    parse_realtime_message,
)
from danta.domain.market import MarketRisk
from danta.services.market_signal import RollingMarketSignal


def _frame(tr_id: str, columns: tuple[str, ...], rows: list[dict[str, str]]) -> str:
    values: list[str] = []
    for row in rows:
        values.extend(row.get(column, "0") for column in columns)
    return f"0|{tr_id}|{len(rows)}|{'^'.join(values)}"


def test_trade_and_orderbook_frames_map_official_columns() -> None:
    observed_at = datetime.now(UTC)
    trade_row = {
        "symbol": "005930",
        "price": "70000",
        "ask1": "70100",
        "bid1": "70000",
        "trade_volume": "15",
        "accumulated_value": "123456789",
        "sell_trade_count": "40",
        "buy_trade_count": "60",
        "trade_strength": "110.5",
        "ask_qty1": "100",
        "bid_qty1": "200",
        "total_ask_qty": "1000",
        "total_bid_qty": "1200",
    }
    parsed = parse_realtime_message(
        _frame(TRADE_TR_ID, TRADE_COLUMNS, [trade_row]),
        received_at=observed_at,
    )
    assert parsed == [
        TradeTick(
            symbol="005930",
            observed_at=observed_at,
            price=70000,
            best_ask=70100,
            best_bid=70000,
            trade_volume=15,
            accumulated_value=123456789,
            sell_trade_count=40,
            buy_trade_count=60,
            trade_strength=Decimal("110.5"),
            ask_quantity=100,
            bid_quantity=200,
            total_ask_quantity=1000,
            total_bid_quantity=1200,
            change_rate=Decimal("0"),
        )
    ]

    orderbook_row = {
        "symbol": "005930",
        "total_ask_qty": "5500",
        "total_bid_qty": "6500",
    }
    for index in range(1, 11):
        orderbook_row[f"ask{index}"] = str(70000 + index * 100)
        orderbook_row[f"bid{index}"] = str(70100 - index * 100)
        orderbook_row[f"ask_qty{index}"] = str(index * 10)
        orderbook_row[f"bid_qty{index}"] = str(index * 20)
    book = parse_realtime_message(
        _frame(ORDERBOOK_TR_ID, ORDERBOOK_COLUMNS, [orderbook_row]),
        received_at=observed_at,
    )[0]
    assert isinstance(book, OrderBookTick)
    assert book.best_ask == 70100
    assert book.best_bid == 70000
    assert book.total_bid_quantity == 6500


def test_rolling_signal_builds_normalized_snapshot() -> None:
    observed_at = datetime.now(UTC)
    signal = RollingMarketSignal("005930")
    for index in range(12):
        signal.update(
            TradeTick(
                symbol="005930",
                observed_at=observed_at,
                price=70000 + index * 10,
                best_ask=70100,
                best_bid=70000,
                trade_volume=10,
                accumulated_value=1000 + index,
                sell_trade_count=40,
                buy_trade_count=60,
                trade_strength=Decimal("120"),
                ask_quantity=100,
                bid_quantity=200,
                total_ask_quantity=1000,
                total_bid_quantity=1500,
            )
        )
    snapshot = signal.snapshot(
        now=observed_at,
        market_risk=MarketRisk.NORMAL,
        market_stress_score=Decimal("0.1"),
        box_valid=True,
        data_fresh=True,
    )
    assert snapshot.symbol == "005930"
    assert Decimal("0") <= snapshot.sell_pressure_score <= Decimal("1")
    assert Decimal("0") <= snapshot.buy_recovery_score <= Decimal("1")


def test_nxt_trade_and_orderbook_frames_preserve_venue() -> None:
    observed_at = datetime.now(UTC)
    trade = parse_realtime_message(
        _frame(
            NXT_TRADE_TR_ID,
            TRADE_COLUMNS,
            [
                {
                    "symbol": "000660",
                    "price": "1800000",
                    "change_rate": "-3.25",
                    "ask1": "1801000",
                    "bid1": "1800000",
                    "trade_strength": "72.1",
                }
            ],
        ),
        received_at=observed_at,
    )[0]
    assert isinstance(trade, TradeTick)
    assert trade.venue is MarketVenue.NXT
    assert trade.change_rate == Decimal("-3.25")

    row = {"symbol": "000660"}
    for index in range(1, 11):
        row[f"ask{index}"] = str(1800000 + index * 1000)
        row[f"bid{index}"] = str(1801000 - index * 1000)
    book = parse_realtime_message(
        _frame(NXT_ORDERBOOK_TR_ID, NXT_ORDERBOOK_COLUMNS, [row]),
        received_at=observed_at,
    )[0]
    assert isinstance(book, OrderBookTick)
    assert book.venue is MarketVenue.NXT


def test_krx_expected_price_frame_maps_official_contract() -> None:
    observed_at = datetime.now(UTC)
    event = parse_realtime_message(
        _frame(
            EXPECTED_TRADE_TR_ID,
            EXPECTED_TRADE_COLUMNS,
            [
                {
                    "symbol": "005930",
                    "price": "240000",
                    "change_rate": "-4.0",
                    "ask1": "240500",
                    "bid1": "240000",
                    "accumulated_volume": "12345",
                }
            ],
        ),
        received_at=observed_at,
    )[0]
    assert event == ExpectedPriceTick(
        symbol="005930",
        observed_at=observed_at,
        expected_price=240000,
        best_ask=240500,
        best_bid=240000,
        expected_volume=12345,
        change_rate=Decimal("-4.0"),
        venue=MarketVenue.KRX,
    )
