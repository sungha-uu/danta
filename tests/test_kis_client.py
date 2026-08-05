from __future__ import annotations

import json
from decimal import Decimal
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
async def test_current_price_supports_nxt_market_division(
    credentials: KisCredentials,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.url.params["FID_COND_MRKT_DIV_CODE"] == "NX"
        return httpx.Response(
            200,
            json={"rt_cd": "0", "output": {"stck_prpr": "81200", "prdy_ctrt": "-1.2"}},
        )

    client = KisClient(credentials, transport=httpx.MockTransport(handler))
    try:
        quote = await client.current_price("005930", market_division="NX")
        with pytest.raises(ValueError, match="J, NX, or UN"):
            await client.current_price("005930", market_division="INVALID")
    finally:
        await client.close()
    assert quote.change_rate == Decimal("-1.2")


@pytest.mark.asyncio
async def test_market_wide_kis_contracts_are_mapped(
    credentials: KisCredentials,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if request.url.path.endswith("/inquire-index-price"):
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": {
                        "bstp_nmix_prpr": "5000.12",
                        "bstp_nmix_prdy_ctrt": "-3.20",
                        "bstp_nmix_oprc": "5150",
                        "bstp_nmix_hgpr": "5160",
                        "bstp_nmix_lwpr": "4980",
                        "acml_tr_pbmn": "30000000",
                        "ascn_issu_cnt": "100",
                        "stnr_issu_cnt": "20",
                        "down_issu_cnt": "800",
                        "uplm_issu_cnt": "1",
                        "lslm_issu_cnt": "2",
                    },
                },
            )
        if request.url.path.endswith("/inquire-investor-time-by-market"):
            assert request.url.params["FID_INPUT_ISCD"] == "KSP"
            assert request.url.params["FID_INPUT_ISCD_2"] == "0001"
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": [
                        {
                            "prsn_ntby_tr_pbmn": "500000",
                            "frgn_ntby_tr_pbmn": "-400000",
                            "orgn_ntby_tr_pbmn": "-100000",
                            "scrt_ntby_tr_pbmn": "-150000",
                            "insu_ntby_tr_pbmn": "10000",
                            "ivtr_ntby_tr_pbmn": "5000",
                            "pe_fund_ntby_tr_pbmn": "1000",
                            "bank_ntby_tr_pbmn": "0",
                            "mrbn_ntby_tr_pbmn": "0",
                            "fund_ntby_tr_pbmn": "34000",
                            "etc_corp_ntby_tr_pbmn": "0",
                        }
                    ],
                },
            )
        assert request.url.params["EXCH_DIV_CLS_CODE"] == "J"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": [
                    {
                        "invr_cls_code": "8888",
                        "arbt_ntby_amt": "-10000",
                        "nabt_ntby_amt": "-90000",
                        "all_ntby_amt": "-100000",
                    }
                ],
            },
        )

    client = KisClient(credentials, transport=httpx.MockTransport(handler))
    client._minimum_rest_interval = 0  # noqa: SLF001
    try:
        index = await client.kospi_index_price()
        investor = await client.kospi_investor_flows()
        program = await client.kospi_program_flows()
    finally:
        await client.close()
    assert index.return_pct == Decimal("-3.20")
    assert index.declining_issues == 800
    assert investor.foreign == -400_000
    assert investor.pension_fund_etc == 34_000
    assert program.total == -100_000


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
async def test_paper_account_snapshot_uses_balance_summary(
    credentials: KisCredentials,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
            )
        assert request.headers["tr_id"] == "VTTC8434R"
        assert request.url.params["INQR_DVSN"] == "02"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": [
                    {
                        "pdno": "005930",
                        "hldg_qty": "2",
                        "ord_psbl_qty": "2",
                        "pchs_avg_pric": "200000",
                    }
                ],
                "output2": [
                    {
                        "dnca_tot_amt": "1000000",
                        "scts_evlu_amt": "420000",
                        "tot_evlu_amt": "1420000",
                        "nass_amt": "1420000",
                        "pchs_amt_smtl_amt": "400000",
                        "evlu_amt_smtl_amt": "420000",
                        "evlu_pfls_smtl_amt": "20000",
                        "asst_icdc_amt": "10000",
                        "asst_icdc_erng_rt": "0.71",
                        "thdt_buy_amt": "400000",
                        "thdt_sll_amt": "0",
                    }
                ],
            },
        )

    client = KisClient(credentials, transport=httpx.MockTransport(handler))
    try:
        snapshot = await client.account_snapshot()
        positions = await client.positions()
    finally:
        await client.close()

    assert snapshot.summary.net_asset_amount == 1_420_000
    assert snapshot.summary.holdings_profit_loss == 20_000
    assert snapshot.summary.asset_change_return_pct == Decimal("0.71")
    assert snapshot.positions[0].symbol == "005930"
    assert positions[0].quantity == 2


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


async def test_daily_order_status_maps_reconciliation_fields(
    credentials: KisCredentials,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(
                200,
                json={
                    "access_token": "token",
                    "access_token_token_expired": "2099-01-01 00:00:00",
                },
            )
        assert request.url.path.endswith("/inquire-daily-ccld")
        assert request.headers["tr_id"] == "VTTC0081R"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": [
                    {
                        "odno": "12345",
                        "orgn_odno": "",
                        "pdno": "005930",
                        "sll_buy_dvsn_cd_name": "현금매수",
                        "ord_qty": "10",
                        "tot_ccld_qty": "4",
                        "rmn_qty": "6",
                        "ord_unpr": "70000",
                        "avg_prvs": "69950",
                        "ord_tmd": "101010",
                        "ord_gno_brno": "06010",
                    },
                    {
                        "odno": "12346",
                        "orgn_odno": "12345",
                        "pdno": "005930",
                        "sll_buy_dvsn_cd_name": "현금매수",
                        "ord_qty": "10",
                        "tot_ccld_qty": "6",
                        "rmn_qty": "-2",
                        "ord_unpr": "70000",
                        "avg_prvs": "69900",
                        "ord_tmd": "101011",
                        "ord_gno_brno": "06010",
                    },
                ],
            },
        )

    client = KisClient(credentials, transport=httpx.MockTransport(handler))
    try:
        statuses = await client.daily_order_statuses(trading_date="20260728", symbol="005930")
    finally:
        await client.close()
    assert statuses[0].filled_quantity == 4
    assert statuses[0].remaining_quantity == 6
    assert statuses[0].average_fill_price == Decimal("69950")
    assert statuses[0].side == "BUY"
    assert statuses[1].filled_quantity == 6
    assert statuses[1].remaining_quantity == 0


async def test_cancel_order_uses_official_paper_contract(
    credentials: KisCredentials,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(
                200,
                json={
                    "access_token": "token",
                    "access_token_token_expired": "2099-01-01 00:00:00",
                },
            )
        assert request.url.path.endswith("/order-rvsecncl")
        assert request.headers["tr_id"] == "VTTC0013U"
        body = json.loads(request.content)
        assert body["RVSE_CNCL_DVSN_CD"] == "02"
        assert body["QTY_ALL_ORD_YN"] == "Y"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output": {
                    "ODNO": "54321",
                    "ORD_TMD": "101111",
                    "KRX_FWDG_ORD_ORGNO": "06010",
                },
            },
        )

    client = KisClient(
        credentials,
        transport=httpx.MockTransport(handler),
        order_submission_enabled=True,
    )
    try:
        receipt = await client.cancel_cash_order(
            broker_order_no="12345", branch_no="06010", quantity=6
        )
    finally:
        await client.close()
    assert receipt.broker_order_no == "54321"


async def test_live_buy_order_uses_official_production_contract() -> None:
    credentials = KisCredentials(
        environment="prod",
        app_key="key",
        app_secret="secret",
        account_no="12345678",
        product_code="01",
        hts_id="tester",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(
                200,
                json={
                    "access_token": "token",
                    "access_token_token_expired": "2099-01-01 00:00:00",
                },
            )
        assert request.headers["tr_id"] == "TTTC0012U"
        return httpx.Response(
            200,
            json={"rt_cd": "0", "output": {"ODNO": "10001", "ORD_TMD": "090001"}},
        )

    client = KisClient(
        credentials,
        transport=httpx.MockTransport(handler),
        order_submission_enabled=True,
    )
    try:
        receipt = await client.submit_cash_order(
            side="BUY", symbol="005930", quantity=1, order_type="LIMIT", limit_price=240_000
        )
    finally:
        await client.close()
    assert receipt.broker_order_no == "10001"


async def test_live_cancel_order_uses_official_production_contract() -> None:
    credentials = KisCredentials(
        environment="prod",
        app_key="key",
        app_secret="secret",
        account_no="12345678",
        product_code="01",
        hts_id="tester",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(
                200,
                json={
                    "access_token": "token",
                    "access_token_token_expired": "2099-01-01 00:00:00",
                },
            )
        assert request.headers["tr_id"] == "TTTC0013U"
        return httpx.Response(
            200,
            json={"rt_cd": "0", "output": {"ODNO": "10002", "ORD_TMD": "090002"}},
        )

    client = KisClient(
        credentials,
        transport=httpx.MockTransport(handler),
        order_submission_enabled=True,
    )
    try:
        receipt = await client.cancel_cash_order(
            broker_order_no="10001", branch_no="06010", quantity=1
        )
    finally:
        await client.close()
    assert receipt.broker_order_no == "10002"


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


@pytest.mark.asyncio
async def test_transient_disconnect_retries_safe_get_once(
    credentials: KisCredentials,
) -> None:
    quote_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal quote_calls
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
            )
        quote_calls += 1
        if quote_calls == 1:
            raise httpx.RemoteProtocolError(
                "server disconnected",
                request=request,
            )
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
    assert quote_calls == 2
