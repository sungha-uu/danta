from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from danta.adapters.kis.client import KisClient
from danta.config import KisCredentials


@pytest.fixture
def credentials() -> KisCredentials:
    return KisCredentials.model_validate(
        {
            "environment": "paper",
            "app_key": "app-key",
            "app_secret": "app-secret",
            "account_no": "12345678",
            "product_code": "01",
            "hts_id": "user",
        }
    )


@pytest.mark.asyncio
async def test_paper_client_uses_vts_and_maps_quote(credentials: KisCredentials) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.headers["tr_id"] == "FHKST01010100"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output": {"stck_prpr": "81200", "prdy_ctrt": "1.23"},
            },
        )

    client = KisClient(credentials, transport=httpx.MockTransport(handler))
    try:
        quote = await client.current_price("005930")
    finally:
        await client.close()
    assert client.base_url == "https://openapivts.koreainvestment.com:29443"
    assert quote.price == 81_200


@pytest.mark.asyncio
async def test_invalid_symbol_never_calls_network(credentials: KisCredentials) -> None:
    client = KisClient(credentials, transport=httpx.MockTransport(lambda _: None))
    try:
        with pytest.raises(ValueError, match="six uppercase alphanumeric"):
            await client.current_price("5930")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_kospi_alphanumeric_short_code_is_supported(
    credentials: KisCredentials,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.url.params["FID_INPUT_ISCD"] == "0126Z0"
        return httpx.Response(
            200,
            json={"rt_cd": "0", "output": {"stck_prpr": "388500", "prdy_ctrt": "0"}},
        )

    client = KisClient(credentials, transport=httpx.MockTransport(handler))
    try:
        quote = await client.current_price("0126Z0")
    finally:
        await client.close()

    assert quote.price == 388_500


@pytest.mark.asyncio
async def test_daily_chart_uses_official_contract(credentials: KisCredentials) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.url.path.endswith("/inquire-daily-itemchartprice")
        assert request.headers["tr_id"] == "FHKST03010100"
        assert request.url.params["FID_INPUT_ISCD"] == "005930"
        assert request.url.params["FID_PERIOD_DIV_CODE"] == "D"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output2": [
                    {
                        "stck_bsop_date": "20260724",
                        "stck_clpr": "249500",
                        "acml_vol": "26175580",
                        "acml_tr_pbmn": "6628392525500",
                    }
                ],
            },
        )

    client = KisClient(credentials, transport=httpx.MockTransport(handler))
    try:
        bars = await client.daily_bars(
            "005930",
            start_date="20260701",
            end_date="20260724",
        )
    finally:
        await client.close()

    assert bars[0].trading_date == "20260724"
    assert bars[0].close == 249_500


@pytest.mark.asyncio
async def test_minute_chart_pages_and_deduplicates(credentials: KisCredentials) -> None:
    page_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal page_calls
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        page_calls += 1
        assert request.url.path.endswith("/inquire-time-dailychartprice")
        assert request.headers["tr_id"] == "FHKST03010230"
        assert request.url.params["FID_INPUT_DATE_1"] == "20260724"
        if page_calls == 1:
            rows = [
                {
                    "stck_bsop_date": "20260724",
                    "stck_cntg_hour": f"13{minute:02d}00",
                    "stck_oprc": "100",
                    "stck_hgpr": "103",
                    "stck_lwpr": "99",
                    "stck_prpr": "102",
                    "cntg_vol": "10",
                    "acml_tr_pbmn": "1000",
                }
                for minute in range(60)
            ] * 2
        else:
            rows = [
                {
                    "stck_bsop_date": "20260724",
                    "stck_cntg_hour": "090000",
                    "stck_oprc": "98",
                    "stck_hgpr": "101",
                    "stck_lwpr": "97",
                    "stck_prpr": "100",
                    "cntg_vol": "20",
                    "acml_tr_pbmn": "200",
                }
            ]
        return httpx.Response(200, json={"rt_cd": "0", "output2": rows})

    client = KisClient(credentials, transport=httpx.MockTransport(handler))
    client._minimum_rest_interval = 0  # noqa: SLF001 - deterministic transport test
    try:
        bars = await client.minute_bars_for_day("005930", trading_date="20260724")
    finally:
        await client.close()

    assert page_calls == 2
    assert bars[0].trading_time == "090000"
    assert bars[-1].trading_time == "135900"
    assert len(bars) == 61


@pytest.mark.asyncio
async def test_order_submission_is_locked_before_network(credentials: KisCredentials) -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = KisClient(credentials, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PermissionError, match="locked"):
            await client.submit_cash_order(
                side="BUY",
                symbol="005930",
                quantity=1,
                order_type="LIMIT",
                limit_price=249_500,
            )
    finally:
        await client.close()

    assert calls == 0


@pytest.mark.asyncio
async def test_paper_orderable_cash_uses_no_credit_fields(
    credentials: KisCredentials,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.headers["tr_id"] == "VTTC8908R"
        assert request.url.params["ORD_DVSN"] == "01"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output": {"nrcvb_buy_amt": "10000000", "nrcvb_buy_qty": "40"},
            },
        )

    client = KisClient(credentials, transport=httpx.MockTransport(handler))
    try:
        orderable = await client.orderable_cash("005930", reference_price=249_500)
    finally:
        await client.close()

    assert orderable.amount == 10_000_000
    assert orderable.quantity == 40


@pytest.mark.asyncio
async def test_enabled_paper_limit_buy_maps_official_order_contract(
    credentials: KisCredentials,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.headers["tr_id"] == "VTTC0012U"
        body = json.loads(request.content)
        assert body["ORD_DVSN"] == "00"
        assert body["ORD_QTY"] == "2"
        assert body["ORD_UNPR"] == "249500"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output": {"ODNO": "0000123456", "ORD_TMD": "090001"},
            },
        )

    client = KisClient(
        credentials,
        transport=httpx.MockTransport(handler),
        order_submission_enabled=True,
    )
    try:
        receipt = await client.submit_cash_order(
            side="BUY",
            symbol="005930",
            quantity=2,
            order_type="LIMIT",
            limit_price=249_500,
        )
    finally:
        await client.close()

    assert receipt.broker_order_no == "0000123456"


@pytest.mark.asyncio
async def test_access_token_is_persisted_and_reused(
    credentials: KisCredentials, tmp_path: Path
) -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"access_token": "cached-token", "expires_in": 3600})

    cache_path = tmp_path / "token.json"
    first = KisClient(
        credentials,
        transport=httpx.MockTransport(handler),
        token_cache_path=cache_path,
    )
    try:
        assert await first.access_token() == "cached-token"
    finally:
        await first.close()

    second = KisClient(
        credentials,
        transport=httpx.MockTransport(handler),
        token_cache_path=cache_path,
    )
    try:
        assert await second.access_token() == "cached-token"
    finally:
        await second.close()
    assert calls == 1


@pytest.mark.asyncio
async def test_expired_token_refreshes_once_for_safe_get(
    credentials: KisCredentials,
) -> None:
    token_calls = 0
    quote_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, quote_calls
        if request.url.path == "/oauth2/tokenP":
            token_calls += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{token_calls}",
                    "expires_in": 3600,
                },
            )
        quote_calls += 1
        if quote_calls == 1:
            return httpx.Response(
                401,
                json={"msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."},
            )
        assert request.headers["Authorization"] == "Bearer token-2"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output": {"stck_prpr": "250000", "prdy_ctrt": "1.2"},
            },
        )

    client = KisClient(credentials, transport=httpx.MockTransport(handler))
    client._minimum_rest_interval = 0
    try:
        quote = await client.current_price("005930")
    finally:
        await client.close()

    assert quote.price == 250000
    assert token_calls == 2
    assert quote_calls == 2
