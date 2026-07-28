from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from danta.config import KisCredentials, TradingEnvironment
from danta.ports.broker import AccountPosition, Quote

TOKEN_PATH = "/oauth2/tokenP"
WS_APPROVAL_PATH = "/oauth2/Approval"
PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
DAILY_CHART_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
MINUTE_DAILY_CHART_PATH = (
    "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
)
BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
ORDERABLE_PATH = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"
CASH_ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
DAILY_ORDER_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
REVISE_CANCEL_PATH = "/uapi/domestic-stock/v1/trading/order-rvsecncl"


class KisApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class _Token:
    value: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class KisDailyBar:
    trading_date: str
    close: int
    volume: int
    trading_value: int


@dataclass(frozen=True, slots=True)
class KisMinuteBar:
    trading_date: str
    trading_time: str
    open: int
    high: int
    low: int
    close: int
    volume: int
    accumulated_trading_value: int


@dataclass(frozen=True, slots=True)
class OrderableCash:
    amount: int
    quantity: int


@dataclass(frozen=True, slots=True)
class CashOrderReceipt:
    broker_order_no: str
    order_time: str
    branch_no: str = ""


@dataclass(frozen=True, slots=True)
class KisOrderStatus:
    broker_order_no: str
    original_order_no: str
    symbol: str
    side: str
    ordered_quantity: int
    filled_quantity: int
    remaining_quantity: int
    order_price: int
    average_fill_price: Decimal
    order_time: str
    branch_no: str


class KisClient:
    def __init__(
        self,
        credentials: KisCredentials,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        token_cache_path: Path | None = None,
        order_submission_enabled: bool = False,
    ) -> None:
        self.credentials = credentials
        self.base_url = (
            "https://openapivts.koreainvestment.com:29443"
            if credentials.environment is TradingEnvironment.PAPER
            else "https://openapi.koreainvestment.com:9443"
        )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_seconds,
            transport=transport,
        )
        self._token_cache_path = token_cache_path
        self._order_submission_enabled = order_submission_enabled
        self._token = self._load_cached_token()
        self._token_lock = asyncio.Lock()
        self._rest_lock = asyncio.Lock()
        self._last_rest_request_at = 0.0
        self._minimum_rest_interval = (
            1.1 if credentials.environment is TradingEnvironment.PAPER else 0.0
        )

    async def __aenter__(self) -> KisClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def access_token(self) -> str:
        now = datetime.now(UTC)
        if self._token and self._token.expires_at > now + timedelta(minutes=1):
            return self._token.value
        async with self._token_lock:
            now = datetime.now(UTC)
            if self._token and self._token.expires_at > now + timedelta(minutes=1):
                return self._token.value
            response = await self._client.post(
                TOKEN_PATH,
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.credentials.app_key.get_secret_value(),
                    "appsecret": self.credentials.app_secret.get_secret_value(),
                },
                headers={"Content-Type": "application/json"},
            )
            body = self._json_or_error(response, "KIS token request failed")
            token = str(body.get("access_token", ""))
            if not token:
                raise KisApiError("KIS token response did not include access_token")
            expires_in = int(body.get("expires_in", 3600))
            self._token = _Token(token, now + timedelta(seconds=expires_in))
            self._save_cached_token(self._token)
            return token

    async def websocket_approval_key(self) -> str:
        response = await self._client.post(
            WS_APPROVAL_PATH,
            json={
                "grant_type": "client_credentials",
                "appkey": self.credentials.app_key.get_secret_value(),
                "secretkey": self.credentials.app_secret.get_secret_value(),
            },
            headers={"Content-Type": "application/json"},
        )
        body = self._json_or_error(response, "KIS WebSocket approval request failed")
        approval_key = str(body.get("approval_key", ""))
        if not approval_key:
            raise KisApiError("KIS WebSocket response did not include approval_key")
        return approval_key

    async def current_price(
        self,
        symbol: str,
        *,
        market_division: str = "J",
    ) -> Quote:
        self._validate_symbol(symbol)
        self._validate_market_division(market_division)
        body = await self._authorized_request(
            "GET",
            PRICE_PATH,
            tr_id="FHKST01010100",
            params={
                "FID_COND_MRKT_DIV_CODE": market_division,
                "FID_INPUT_ISCD": symbol,
            },
        )
        output = body.get("output")
        if not isinstance(output, dict):
            raise KisApiError("KIS price response did not include output")
        return Quote(
            symbol=symbol,
            price=int(output["stck_prpr"]),
            change_rate=(
                Decimal(str(output["prdy_ctrt"]))
                if output.get("prdy_ctrt") not in (None, "")
                else None
            ),
            raw_timestamp=output.get("stck_cntg_hour"),
        )

    async def daily_bars(
        self,
        symbol: str,
        *,
        start_date: str,
        end_date: str,
    ) -> list[KisDailyBar]:
        self._validate_symbol(symbol)
        self._validate_date(start_date)
        self._validate_date(end_date)
        if start_date > end_date:
            raise ValueError("start_date must not be after end_date")
        body = await self._authorized_request(
            "GET",
            DAILY_CHART_PATH,
            tr_id="FHKST03010100",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": start_date,
                "FID_INPUT_DATE_2": end_date,
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        rows = body.get("output2")
        if not isinstance(rows, list):
            raise KisApiError("KIS daily chart response did not include output2")
        result: list[KisDailyBar] = []
        for row in rows:
            if not isinstance(row, dict):
                raise KisApiError("KIS daily chart response contains an invalid row")
            if not row.get("stck_bsop_date") or not row.get("stck_clpr"):
                continue
            result.append(
                KisDailyBar(
                    trading_date=str(row["stck_bsop_date"]),
                    close=int(row["stck_clpr"]),
                    volume=int(row.get("acml_vol", "0") or "0"),
                    trading_value=int(row.get("acml_tr_pbmn", "0") or "0"),
                )
            )
        if not result:
            raise KisApiError("KIS daily chart response contained no price bars")
        return result

    async def minute_bars_for_day(
        self,
        symbol: str,
        *,
        trading_date: str,
    ) -> list[KisMinuteBar]:
        """Fetch one regular KRX session, paging backward in 120-row chunks."""
        self._validate_symbol(symbol)
        self._validate_date(trading_date)
        current_time = "153000"
        unique: dict[str, KisMinuteBar] = {}
        for _ in range(10):
            body = await self._authorized_request(
                "GET",
                MINUTE_DAILY_CHART_PATH,
                tr_id="FHKST03010230",
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": symbol,
                    "FID_INPUT_HOUR_1": current_time,
                    "FID_INPUT_DATE_1": trading_date,
                    "FID_PW_DATA_INCU_YN": "Y",
                    "FID_FAKE_TICK_INCU_YN": "",
                },
            )
            rows = body.get("output2")
            if not isinstance(rows, list):
                raise KisApiError("KIS minute chart response did not include output2")
            page: list[KisMinuteBar] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise KisApiError("KIS minute chart response contains an invalid row")
                row_date = str(row.get("stck_bsop_date", ""))
                row_time = str(row.get("stck_cntg_hour", ""))
                if row_date != trading_date or not ("090000" <= row_time <= "153000"):
                    continue
                try:
                    bar = KisMinuteBar(
                        trading_date=row_date,
                        trading_time=row_time,
                        open=int(row.get("stck_oprc", "0") or "0"),
                        high=int(row.get("stck_hgpr", "0") or "0"),
                        low=int(row.get("stck_lwpr", "0") or "0"),
                        close=int(row.get("stck_prpr", "0") or "0"),
                        volume=int(row.get("cntg_vol", "0") or "0"),
                        accumulated_trading_value=int(
                            row.get("acml_tr_pbmn", "0") or "0"
                        ),
                    )
                except (TypeError, ValueError) as exc:
                    raise KisApiError("KIS minute chart response has invalid numbers") from exc
                if min(bar.open, bar.high, bar.low, bar.close) <= 0:
                    continue
                page.append(bar)
                unique[row_time] = bar
            if not page:
                break
            earliest = min(item.trading_time for item in page)
            if earliest <= "090000" or len(rows) < 120:
                break
            if earliest >= current_time:
                # A final historical page can repeat one row for this date and
                # fill the remainder with the prior session. The store's regular
                # session coverage gate decides whether the day is complete.
                break
            current_time = earliest
        result = sorted(unique.values(), key=lambda item: item.trading_time)
        if not result:
            raise KisApiError(
                f"KIS minute chart response contained no bars for {symbol} {trading_date}"
            )
        return result

    async def positions(self) -> list[AccountPosition]:
        tr_id = (
            "VTTC8434R"
            if self.credentials.environment is TradingEnvironment.PAPER
            else "TTTC8434R"
        )
        body = await self._authorized_request(
            "GET",
            BALANCE_PATH,
            tr_id=tr_id,
            params={
                "CANO": self.credentials.account_no,
                "ACNT_PRDT_CD": self.credentials.product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        rows = body.get("output1", [])
        if not isinstance(rows, list):
            raise KisApiError("KIS balance response output1 is invalid")
        result: list[AccountPosition] = []
        for row in rows:
            quantity = int(row.get("hldg_qty", "0"))
            if quantity <= 0:
                continue
            result.append(
                AccountPosition(
                    symbol=str(row["pdno"]),
                    quantity=quantity,
                    sellable_quantity=int(row.get("ord_psbl_qty", "0")),
                    average_price=Decimal(str(row.get("pchs_avg_pric", "0"))),
                )
            )
        return result

    async def orderable_cash(self, symbol: str, *, reference_price: int) -> OrderableCash:
        self._validate_symbol(symbol)
        if reference_price <= 0:
            raise ValueError("reference_price must be positive")
        tr_id = (
            "VTTC8908R"
            if self.credentials.environment is TradingEnvironment.PAPER
            else "TTTC8908R"
        )
        body = await self._authorized_request(
            "GET",
            ORDERABLE_PATH,
            tr_id=tr_id,
            params={
                "CANO": self.credentials.account_no,
                "ACNT_PRDT_CD": self.credentials.product_code,
                "PDNO": symbol,
                "ORD_UNPR": str(reference_price),
                "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
        )
        output = body.get("output")
        if not isinstance(output, dict):
            raise KisApiError("KIS orderable cash response did not include output")
        return OrderableCash(
            amount=int(output.get("nrcvb_buy_amt", "0") or "0"),
            quantity=int(output.get("nrcvb_buy_qty", "0") or "0"),
        )

    async def submit_cash_order(
        self,
        *,
        side: str,
        symbol: str,
        quantity: int,
        order_type: str,
        limit_price: int | None = None,
    ) -> CashOrderReceipt:
        if not self._order_submission_enabled:
            raise PermissionError("KIS order submission is locked")
        if self.credentials.environment is TradingEnvironment.PROD:
            raise PermissionError("KIS production order submission is locked during Phase 0")
        self._validate_symbol(symbol)
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if order_type not in {"MARKET", "LIMIT"}:
            raise ValueError("order_type must be MARKET or LIMIT")
        if order_type == "LIMIT" and (limit_price is None or limit_price <= 0):
            raise ValueError("positive limit_price is required for LIMIT orders")
        tr_id = "VTTC0012U" if side == "BUY" else "VTTC0011U"
        body = await self._authorized_request(
            "POST",
            CASH_ORDER_PATH,
            tr_id=tr_id,
            json_body={
                "CANO": self.credentials.account_no,
                "ACNT_PRDT_CD": self.credentials.product_code,
                "PDNO": symbol,
                "ORD_DVSN": "01" if order_type == "MARKET" else "00",
                "ORD_QTY": str(quantity),
                "ORD_UNPR": "0" if order_type == "MARKET" else str(limit_price),
                "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": "01" if side == "SELL" else "",
                "CNDT_PRIC": "",
            },
        )
        output = body.get("output")
        if not isinstance(output, dict) or not output.get("ODNO"):
            raise KisApiError("KIS cash order response did not include an order number")
        return CashOrderReceipt(
            broker_order_no=str(output["ODNO"]),
            order_time=str(output.get("ORD_TMD", "")),
            branch_no=str(output.get("KRX_FWDG_ORD_ORGNO", "")),
        )

    async def daily_order_statuses(
        self,
        *,
        trading_date: str,
        symbol: str = "",
        broker_order_no: str = "",
    ) -> list[KisOrderStatus]:
        self._validate_date(trading_date)
        if symbol:
            self._validate_symbol(symbol)
        tr_id = (
            "VTTC0081R"
            if self.credentials.environment is TradingEnvironment.PAPER
            else "TTTC0081R"
        )
        body = await self._authorized_request(
            "GET",
            DAILY_ORDER_PATH,
            tr_id=tr_id,
            params={
                "CANO": self.credentials.account_no,
                "ACNT_PRDT_CD": self.credentials.product_code,
                "INQR_STRT_DT": trading_date,
                "INQR_END_DT": trading_date,
                "SLL_BUY_DVSN_CD": "00",
                "PDNO": symbol,
                "CCLD_DVSN": "00",
                "INQR_DVSN": "00",
                "INQR_DVSN_3": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": broker_order_no,
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
                "EXCG_ID_DVSN_CD": "KRX",
            },
        )
        rows = body.get("output1", [])
        if not isinstance(rows, list):
            raise KisApiError("KIS daily order response output1 is invalid")
        result: list[KisOrderStatus] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("odno"):
                continue
            side_name = str(row.get("sll_buy_dvsn_cd_name", ""))
            side = "BUY" if "매수" in side_name else "SELL"
            result.append(
                KisOrderStatus(
                    broker_order_no=str(row["odno"]),
                    original_order_no=str(row.get("orgn_odno", "")),
                    symbol=str(row.get("pdno", "")),
                    side=side,
                    ordered_quantity=int(row.get("ord_qty", "0") or "0"),
                    filled_quantity=int(row.get("tot_ccld_qty", "0") or "0"),
                    remaining_quantity=int(row.get("rmn_qty", "0") or "0"),
                    order_price=int(row.get("ord_unpr", "0") or "0"),
                    average_fill_price=Decimal(str(row.get("avg_prvs", "0") or "0")),
                    order_time=str(row.get("ord_tmd", "")),
                    branch_no=str(row.get("ord_gno_brno", "")),
                )
            )
        return result

    async def cancel_cash_order(
        self,
        *,
        broker_order_no: str,
        branch_no: str,
        quantity: int,
    ) -> CashOrderReceipt:
        if not self._order_submission_enabled:
            raise PermissionError("KIS order submission is locked")
        if self.credentials.environment is TradingEnvironment.PROD:
            raise PermissionError("KIS production order submission is locked during Phase 0")
        if not broker_order_no or not branch_no:
            raise ValueError("broker_order_no and branch_no are required")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        body = await self._authorized_request(
            "POST",
            REVISE_CANCEL_PATH,
            tr_id="VTTC0013U",
            json_body={
                "CANO": self.credentials.account_no,
                "ACNT_PRDT_CD": self.credentials.product_code,
                "KRX_FWDG_ORD_ORGNO": branch_no,
                "ORGN_ODNO": broker_order_no,
                "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": str(quantity),
                "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y",
                "EXCG_ID_DVSN_CD": "KRX",
                "CNDT_PRIC": "",
            },
        )
        output = body.get("output")
        if not isinstance(output, dict) or not output.get("ODNO"):
            raise KisApiError("KIS cancel response did not include an order number")
        return CashOrderReceipt(
            broker_order_no=str(output["ODNO"]),
            order_time=str(output.get("ORD_TMD", "")),
            branch_no=str(output.get("KRX_FWDG_ORD_ORGNO", branch_no)),
        )

    async def _authorized_request(
        self,
        method: str,
        path: str,
        *,
        tr_id: str,
        params: dict[str, str] | None = None,
        json_body: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(2):
            token = await self.access_token()
            async with self._rest_lock:
                elapsed = time.monotonic() - self._last_rest_request_at
                wait_seconds = self._minimum_rest_interval - elapsed
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                try:
                    response = await self._client.request(
                        method,
                        path,
                        params=params,
                        json=json_body,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "appkey": self.credentials.app_key.get_secret_value(),
                            "appsecret": self.credentials.app_secret.get_secret_value(),
                            "tr_id": tr_id,
                            "custtype": "P",
                            "Content-Type": "application/json",
                        },
                    )
                except httpx.RequestError as exc:
                    if attempt == 0 and method == "GET":
                        continue
                    if method == "GET":
                        raise KisApiError(
                            f"KIS GET request failed after retry: {path}"
                        ) from exc
                    raise
                self._last_rest_request_at = time.monotonic()
            try:
                raw_body = response.json()
            except ValueError:
                raw_body = None
            error_code = (
                str(raw_body.get("msg_cd", raw_body.get("error_code", "")))
                if isinstance(raw_body, dict)
                else ""
            )
            if (
                attempt == 0
                and method == "GET"
                and error_code == "EGW00123"
            ):
                # A broker-side token can expire before its local expiry timestamp.
                # GET market-data calls are safe to repeat after one forced refresh.
                self._token = None
                continue
            body = self._json_or_error(response, f"KIS request failed: {path}")
            if str(body.get("rt_cd", "0")) != "0":
                raise KisApiError(
                    f"KIS rejected request: {body.get('msg_cd', 'UNKNOWN')} "
                    f"{body.get('msg1', '')}".strip(),
                    status_code=response.status_code,
                )
            return body
        raise KisApiError(f"KIS request failed after token refresh: {path}")

    @staticmethod
    def _json_or_error(response: httpx.Response, message: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise KisApiError(message, status_code=response.status_code) from exc
        if not isinstance(body, dict):
            raise KisApiError(f"{message}: unexpected response")
        if response.is_error:
            error_code = str(body.get("msg_cd", body.get("error_code", "UNKNOWN")))
            error_message = str(body.get("msg1", body.get("error_description", "")))
            detail = f"{error_code} {error_message}".strip()
            raise KisApiError(
                f"{message}: {detail}",
                status_code=response.status_code,
            )
        return body

    def _load_cached_token(self) -> _Token | None:
        if self._token_cache_path is None or not self._token_cache_path.exists():
            return None
        try:
            body = json.loads(self._token_cache_path.read_text(encoding="utf-8"))
            token = str(body["access_token"])
            expires_at = datetime.fromisoformat(str(body["expires_at"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not token or expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
            return None
        return _Token(token, expires_at)

    def _save_cached_token(self, token: _Token) -> None:
        if self._token_cache_path is None:
            return
        self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._token_cache_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(
                {
                    "access_token": token.value,
                    "expires_at": token.expires_at.isoformat(),
                }
            ),
            encoding="utf-8",
        )
        temporary_path.replace(self._token_cache_path)

    @staticmethod
    def _validate_symbol(symbol: str) -> None:
        if len(symbol) != 6 or not symbol.isascii() or not symbol.isalnum():
            raise ValueError(
                "domestic stock symbol must be exactly six uppercase alphanumeric characters"
            )
        if symbol != symbol.upper():
            raise ValueError("domestic stock symbol must use uppercase characters")

    @staticmethod
    def _validate_date(value: str) -> None:
        if len(value) != 8 or not value.isdigit():
            raise ValueError("KIS date must use YYYYMMDD")

    @staticmethod
    def _validate_market_division(value: str) -> None:
        if value not in {"J", "NX", "UN"}:
            raise ValueError("market division must be J, NX, or UN")
