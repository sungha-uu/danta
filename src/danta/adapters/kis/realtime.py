from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

import httpx
import websockets

from danta.config import KisCredentials, TradingEnvironment

TRADE_TR_ID = "H0STCNT0"
ORDERBOOK_TR_ID = "H0STASP0"
EXPECTED_TRADE_TR_ID = "H0STANC0"
NXT_TRADE_TR_ID = "H0NXCNT0"
NXT_ORDERBOOK_TR_ID = "H0NXASP0"
NXT_EXPECTED_TRADE_TR_ID = "H0NXANC0"
APPROVAL_PATH = "/oauth2/Approval"


class MarketVenue(StrEnum):
    KRX = "KRX"
    NXT = "NXT"


TRADE_COLUMNS = (
    "symbol",
    "time",
    "price",
    "change_sign",
    "change",
    "change_rate",
    "weighted_average",
    "open",
    "high",
    "low",
    "ask1",
    "bid1",
    "trade_volume",
    "accumulated_volume",
    "accumulated_value",
    "sell_trade_count",
    "buy_trade_count",
    "net_buy_trade_count",
    "trade_strength",
    "sell_trade_total",
    "buy_trade_total",
    "trade_division",
    "buy_rate",
    "volume_rate",
    "open_time",
    "open_sign",
    "open_change",
    "high_time",
    "high_sign",
    "high_change",
    "low_time",
    "low_sign",
    "low_change",
    "business_date",
    "market_open_code",
    "trading_halt",
    "ask_qty1",
    "bid_qty1",
    "total_ask_qty",
    "total_bid_qty",
    "turnover_rate",
    "previous_same_time_volume",
    "previous_same_time_volume_rate",
    "hour_class",
    "market_time_class",
    "vi_reference_price",
)

ORDERBOOK_COLUMNS = (
    "symbol",
    "time",
    "hour_class",
    *tuple(f"ask{i}" for i in range(1, 11)),
    *tuple(f"bid{i}" for i in range(1, 11)),
    *tuple(f"ask_qty{i}" for i in range(1, 11)),
    *tuple(f"bid_qty{i}" for i in range(1, 11)),
    "total_ask_qty",
    "total_bid_qty",
    "overtime_total_ask_qty",
    "overtime_total_bid_qty",
    "expected_price",
    "expected_quantity",
    "expected_volume",
    "expected_change",
    "expected_change_sign",
    "expected_change_rate",
    "accumulated_volume",
    "total_ask_change",
    "total_bid_change",
    "overtime_ask_change",
    "overtime_bid_change",
    "deal_class",
)
NXT_ORDERBOOK_COLUMNS = ORDERBOOK_COLUMNS + (
    "krx_mid_price",
    "krx_mid_total_quantity",
    "krx_mid_class",
    "nxt_mid_price",
    "nxt_mid_total_quantity",
    "nxt_mid_class",
)
EXPECTED_TRADE_COLUMNS = TRADE_COLUMNS[:-1]
NXT_EXPECTED_TRADE_COLUMNS = TRADE_COLUMNS


@dataclass(frozen=True, slots=True)
class TradeTick:
    symbol: str
    observed_at: datetime
    price: int
    best_ask: int
    best_bid: int
    trade_volume: int
    accumulated_value: int
    sell_trade_count: int
    buy_trade_count: int
    trade_strength: Decimal
    ask_quantity: int
    bid_quantity: int
    total_ask_quantity: int
    total_bid_quantity: int
    venue: MarketVenue = MarketVenue.KRX
    change_rate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OrderBookTick:
    symbol: str
    observed_at: datetime
    best_ask: int
    best_bid: int
    ask_prices: tuple[int, ...]
    bid_prices: tuple[int, ...]
    ask_quantities: tuple[int, ...]
    bid_quantities: tuple[int, ...]
    total_ask_quantity: int
    total_bid_quantity: int
    venue: MarketVenue = MarketVenue.KRX


@dataclass(frozen=True, slots=True)
class ExpectedPriceTick:
    symbol: str
    observed_at: datetime
    expected_price: int
    best_ask: int
    best_bid: int
    expected_volume: int
    change_rate: Decimal | None
    venue: MarketVenue


RealtimeEvent = TradeTick | OrderBookTick | ExpectedPriceTick


def parse_realtime_message(raw: str, *, received_at: datetime) -> list[RealtimeEvent]:
    if received_at.tzinfo is None:
        raise ValueError("received_at must be timezone-aware")
    if not raw or raw[0] not in {"0", "1"}:
        return []
    pieces = raw.split("|", 3)
    if len(pieces) != 4:
        raise ValueError("invalid KIS realtime frame")
    tr_id = pieces[1]
    try:
        count = int(pieces[2])
    except ValueError as exc:
        raise ValueError("invalid KIS realtime record count") from exc
    layouts: dict[str, tuple[tuple[str, ...], MarketVenue, str]] = {
        TRADE_TR_ID: (TRADE_COLUMNS, MarketVenue.KRX, "TRADE"),
        ORDERBOOK_TR_ID: (ORDERBOOK_COLUMNS, MarketVenue.KRX, "ORDERBOOK"),
        EXPECTED_TRADE_TR_ID: (
            EXPECTED_TRADE_COLUMNS,
            MarketVenue.KRX,
            "EXPECTED",
        ),
        NXT_TRADE_TR_ID: (TRADE_COLUMNS, MarketVenue.NXT, "TRADE"),
        NXT_ORDERBOOK_TR_ID: (
            NXT_ORDERBOOK_COLUMNS,
            MarketVenue.NXT,
            "ORDERBOOK",
        ),
        NXT_EXPECTED_TRADE_TR_ID: (
            NXT_EXPECTED_TRADE_COLUMNS,
            MarketVenue.NXT,
            "EXPECTED",
        ),
    }
    layout = layouts.get(tr_id)
    if layout is None:
        return []
    columns, venue, event_type = layout
    values = pieces[3].split("^")
    width = len(columns)
    if tr_id == NXT_ORDERBOOK_TR_ID and count > 0 and len(values) % count == 0:
        observed_width = len(values) // count
        # The official sample currently defines 65 fields. The paper WebSocket
        # has also been observed returning 62 fields with the final NMID triplet
        # omitted. Those optional midpoint fields are not used by our internal
        # order-book contract, whose required prefix is the first 59 fields.
        if len(ORDERBOOK_COLUMNS) <= observed_width <= len(NXT_ORDERBOOK_COLUMNS):
            columns = NXT_ORDERBOOK_COLUMNS[:observed_width]
            width = observed_width
    if count <= 0 or len(values) < count * width:
        raise ValueError(
            "KIS realtime frame has incomplete records "
            f"(tr_id={tr_id}, count={count}, values={len(values)}, width={width})"
        )
    events: list[RealtimeEvent] = []
    for index in range(count):
        row = dict(zip(columns, values[index * width : (index + 1) * width], strict=True))
        if event_type == "TRADE":
            events.append(_trade_tick(row, received_at, venue=venue))
        elif event_type == "ORDERBOOK":
            events.append(_orderbook_tick(row, received_at, venue=venue))
        else:
            events.append(_expected_price_tick(row, received_at, venue=venue))
    return events


def _integer(row: dict[str, str], key: str) -> int:
    try:
        return int(row[key] or "0")
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid KIS realtime integer: {key}") from exc


def _decimal_or_none(row: dict[str, str], key: str) -> Decimal | None:
    value = row.get(key, "")
    if value == "":
        return None
    try:
        return Decimal(value)
    except ValueError as exc:
        raise ValueError(f"invalid KIS realtime decimal: {key}") from exc


def _trade_tick(
    row: dict[str, str],
    received_at: datetime,
    *,
    venue: MarketVenue,
) -> TradeTick:
    return TradeTick(
        symbol=row["symbol"],
        observed_at=received_at,
        price=_integer(row, "price"),
        best_ask=_integer(row, "ask1"),
        best_bid=_integer(row, "bid1"),
        trade_volume=_integer(row, "trade_volume"),
        accumulated_value=_integer(row, "accumulated_value"),
        sell_trade_count=_integer(row, "sell_trade_count"),
        buy_trade_count=_integer(row, "buy_trade_count"),
        trade_strength=Decimal(row["trade_strength"] or "0"),
        ask_quantity=_integer(row, "ask_qty1"),
        bid_quantity=_integer(row, "bid_qty1"),
        total_ask_quantity=_integer(row, "total_ask_qty"),
        total_bid_quantity=_integer(row, "total_bid_qty"),
        venue=venue,
        change_rate=_decimal_or_none(row, "change_rate"),
    )


def _orderbook_tick(
    row: dict[str, str],
    received_at: datetime,
    *,
    venue: MarketVenue,
) -> OrderBookTick:
    asks = tuple(_integer(row, f"ask{i}") for i in range(1, 11))
    bids = tuple(_integer(row, f"bid{i}") for i in range(1, 11))
    ask_quantities = tuple(_integer(row, f"ask_qty{i}") for i in range(1, 11))
    bid_quantities = tuple(_integer(row, f"bid_qty{i}") for i in range(1, 11))
    return OrderBookTick(
        symbol=row["symbol"],
        observed_at=received_at,
        best_ask=asks[0],
        best_bid=bids[0],
        ask_prices=asks,
        bid_prices=bids,
        ask_quantities=ask_quantities,
        bid_quantities=bid_quantities,
        total_ask_quantity=_integer(row, "total_ask_qty"),
        total_bid_quantity=_integer(row, "total_bid_qty"),
        venue=venue,
    )


def _expected_price_tick(
    row: dict[str, str],
    received_at: datetime,
    *,
    venue: MarketVenue,
) -> ExpectedPriceTick:
    return ExpectedPriceTick(
        symbol=row["symbol"],
        observed_at=received_at,
        expected_price=_integer(row, "price"),
        best_ask=_integer(row, "ask1"),
        best_bid=_integer(row, "bid1"),
        expected_volume=_integer(row, "accumulated_volume"),
        change_rate=_decimal_or_none(row, "change_rate"),
        venue=venue,
    )


class KisRealtimeClient:
    """Small official-protocol client for KRX trades and 10-level order books."""

    def __init__(
        self,
        credentials: KisCredentials,
        *,
        http_client: httpx.AsyncClient | None = None,
        maximum_subscriptions: int = 40,
    ) -> None:
        self.credentials = credentials
        self.maximum_subscriptions = maximum_subscriptions
        self._owns_http = http_client is None
        base_url = (
            "https://openapivts.koreainvestment.com:29443"
            if credentials.environment is TradingEnvironment.PAPER
            else "https://openapi.koreainvestment.com:9443"
        )
        self._http = http_client or httpx.AsyncClient(base_url=base_url, timeout=15)
        self._ws_url = (
            "ws://ops.koreainvestment.com:31000/tryitout"
            if credentials.environment is TradingEnvironment.PAPER
            else "ws://ops.koreainvestment.com:21000/tryitout"
        )

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def approval_key(self) -> str:
        response = await self._http.post(
            APPROVAL_PATH,
            json={
                "grant_type": "client_credentials",
                "appkey": self.credentials.app_key.get_secret_value(),
                "secretkey": self.credentials.app_secret.get_secret_value(),
            },
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not body.get("approval_key"):
            raise RuntimeError("KIS websocket approval response is invalid")
        return str(body["approval_key"])

    async def stream(
        self,
        symbols: list[str],
        *,
        venue: MarketVenue = MarketVenue.KRX,
        reconnect_attempts: int = 5,
    ) -> AsyncIterator[RealtimeEvent]:
        tr_ids = (
            (TRADE_TR_ID, ORDERBOOK_TR_ID)
            if venue is MarketVenue.KRX
            else (NXT_TRADE_TR_ID, NXT_ORDERBOOK_TR_ID)
        )
        async for event in self._stream_tr_ids(
            symbols,
            tr_ids=tr_ids,
            reconnect_attempts=reconnect_attempts,
        ):
            yield event

    async def stream_expected_prices(
        self,
        symbols: list[str],
        *,
        venue: MarketVenue = MarketVenue.KRX,
        reconnect_attempts: int = 5,
    ) -> AsyncIterator[ExpectedPriceTick]:
        tr_id = EXPECTED_TRADE_TR_ID if venue is MarketVenue.KRX else NXT_EXPECTED_TRADE_TR_ID
        async for event in self._stream_tr_ids(
            symbols,
            tr_ids=(tr_id,),
            reconnect_attempts=reconnect_attempts,
        ):
            if isinstance(event, ExpectedPriceTick):
                yield event

    async def stream_premarket(
        self,
        symbols: list[str],
        *,
        reconnect_attempts: int = 5,
    ) -> AsyncIterator[RealtimeEvent]:
        """Stream NXT trades/books and KRX expected prices on one socket."""
        async for event in self._stream_tr_ids(
            symbols,
            tr_ids=(
                NXT_TRADE_TR_ID,
                NXT_ORDERBOOK_TR_ID,
                EXPECTED_TRADE_TR_ID,
            ),
            reconnect_attempts=reconnect_attempts,
        ):
            yield event

    async def _stream_tr_ids(
        self,
        symbols: list[str],
        *,
        tr_ids: tuple[str, ...],
        reconnect_attempts: int,
    ) -> AsyncIterator[RealtimeEvent]:
        unique_symbols = list(dict.fromkeys(symbols))
        if not unique_symbols:
            raise ValueError("at least one symbol is required")
        if reconnect_attempts < 0:
            raise ValueError("reconnect_attempts must not be negative")
        subscription_count = len(unique_symbols) * len(tr_ids)
        if subscription_count > self.maximum_subscriptions:
            raise ValueError("KIS websocket subscription limit exceeded")
        approval_key = await self.approval_key()
        attempt = 0
        while attempt <= reconnect_attempts:
            try:
                async with websockets.connect(self._ws_url, ping_interval=20) as socket:
                    for symbol in unique_symbols:
                        for tr_id in tr_ids:
                            await socket.send(
                                json.dumps(
                                    _subscription_message(approval_key, tr_id, symbol),
                                    separators=(",", ":"),
                                )
                            )
                            await asyncio.sleep(0.05)
                    async for message in socket:
                        raw = cast(str, message)
                        if raw.startswith("{"):
                            body = json.loads(raw)
                            if (
                                isinstance(body, dict)
                                and body.get("header", {}).get("tr_id") == "PINGPONG"
                            ):
                                await socket.send(raw)
                            continue
                        for event in parse_realtime_message(raw, received_at=datetime.now(UTC)):
                            yield event
                return
            except (OSError, websockets.WebSocketException):
                attempt += 1
                if attempt > reconnect_attempts:
                    raise
                await asyncio.sleep(min(2**attempt, 30))


def _subscription_message(approval_key: str, tr_id: str, symbol: str) -> dict[str, object]:
    if len(symbol) != 6 or not symbol.isascii() or not symbol.isalnum():
        raise ValueError("invalid domestic stock symbol")
    return {
        "header": {
            "approval_key": approval_key,
            "custtype": "P",
            "tr_type": "1",
            "content-type": "utf-8",
        },
        "body": {"input": {"tr_id": tr_id, "tr_key": symbol}},
    }
