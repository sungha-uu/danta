from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import httpx
import websockets

from danta.config import KisCredentials, TradingEnvironment

TRADE_TR_ID = "H0STCNT0"
ORDERBOOK_TR_ID = "H0STASP0"
APPROVAL_PATH = "/oauth2/Approval"

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


RealtimeEvent = TradeTick | OrderBookTick


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
    columns = TRADE_COLUMNS if tr_id == TRADE_TR_ID else ORDERBOOK_COLUMNS
    if tr_id not in {TRADE_TR_ID, ORDERBOOK_TR_ID}:
        return []
    values = pieces[3].split("^")
    width = len(columns)
    if count <= 0 or len(values) < count * width:
        raise ValueError("KIS realtime frame has incomplete records")
    events: list[RealtimeEvent] = []
    for index in range(count):
        row = dict(zip(columns, values[index * width : (index + 1) * width], strict=True))
        if tr_id == TRADE_TR_ID:
            events.append(_trade_tick(row, received_at))
        else:
            events.append(_orderbook_tick(row, received_at))
    return events


def _integer(row: dict[str, str], key: str) -> int:
    try:
        return int(row[key] or "0")
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid KIS realtime integer: {key}") from exc


def _trade_tick(row: dict[str, str], received_at: datetime) -> TradeTick:
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
    )


def _orderbook_tick(row: dict[str, str], received_at: datetime) -> OrderBookTick:
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
        reconnect_attempts: int = 5,
    ) -> AsyncIterator[RealtimeEvent]:
        unique_symbols = list(dict.fromkeys(symbols))
        if not unique_symbols:
            raise ValueError("at least one symbol is required")
        subscription_count = len(unique_symbols) * 2
        if subscription_count > self.maximum_subscriptions:
            raise ValueError("KIS websocket subscription limit exceeded")
        approval_key = await self.approval_key()
        attempt = 0
        while attempt <= reconnect_attempts:
            try:
                async with websockets.connect(self._ws_url, ping_interval=20) as socket:
                    for symbol in unique_symbols:
                        for tr_id in (TRADE_TR_ID, ORDERBOOK_TR_ID):
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
                        for event in parse_realtime_message(
                            raw, received_at=datetime.now(UTC)
                        ):
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
