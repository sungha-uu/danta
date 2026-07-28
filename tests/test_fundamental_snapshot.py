from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from danta.adapters.dart.financials import OpenDartFinancialClient
from danta.domain.fundamentals import (
    financial_risk_flags,
    health_status_for,
    safe_ratio,
)
from danta.services.fundamental_snapshot import (
    refresh_fundamental_snapshots,
    report_candidates_for,
)


def test_report_calendar_uses_latest_expected_period() -> None:
    assert report_candidates_for(date(2026, 7, 28))[0] == (2026, "11013")
    assert report_candidates_for(date(2026, 9, 1))[0] == (2026, "11012")
    assert report_candidates_for(date(2026, 12, 1))[0] == (2026, "11014")
    assert report_candidates_for(date(2026, 4, 2))[0] == (2025, "11011")


def test_financial_ratios_and_risk_flags_handle_invalid_denominators() -> None:
    assert safe_ratio(Decimal("300"), Decimal("100")) == Decimal("300.00")
    assert safe_ratio(Decimal("1"), Decimal("0")) is None
    flags = financial_risk_flags(
        total_assets=Decimal("100"),
        total_liabilities=Decimal("90"),
        total_equity=Decimal("10"),
        revenue=Decimal("50"),
        operating_income=Decimal("-1"),
        net_income=Decimal("-2"),
        debt_ratio_pct=Decimal("900"),
        current_ratio_pct=Decimal("60"),
    )
    assert flags == (
        "OPERATING_LOSS",
        "NET_LOSS",
        "HIGH_DEBT_RATIO",
        "LOW_CURRENT_RATIO",
    )
    assert health_status_for(flags) == "CAUTION"


@pytest.mark.asyncio
async def test_open_dart_client_splits_more_than_100_companies(tmp_path: Path) -> None:
    request_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        corp_codes = query["corp_code"][0].split(",")
        request_sizes.append(len(corp_codes))
        return httpx.Response(200, json={"status": "000", "list": []})

    client = OpenDartFinancialClient(
        "x" * 40,
        corp_code_cache_path=tmp_path / "corp.json",
        transport=httpx.MockTransport(handler),
    )
    await client.fetch_major_accounts(
        [f"{index:08d}" for index in range(101)],
        business_year=2026,
        report_code="11013",
    )
    assert request_sizes == [100, 1]


@pytest.mark.asyncio
async def test_refresh_builds_independent_snapshot_from_major_accounts(
    tmp_path: Path,
) -> None:
    corp_cache = tmp_path / "corp.json"
    corp_cache.write_text(
        json.dumps({"005930": "00126380"}),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        assert query["bsns_year"] == ["2026"]
        assert query["reprt_code"] == ["11013"]
        base = {
            "corp_code": "00126380",
            "stock_code": "005930",
            "fs_div": "CFS",
            "rcept_no": "20260515000001",
            "currency": "KRW",
            "thstrm_add_amount": "",
        }
        accounts = {
            "자산총계": "1000",
            "유동자산": "500",
            "부채총계": "400",
            "유동부채": "200",
            "자본총계": "600",
            "매출액": "800",
            "영업이익": "80",
            "당기순이익": "60",
        }
        rows = []
        for name, amount in accounts.items():
            row = {**base, "account_nm": name, "thstrm_amount": amount}
            if name in {"매출액", "영업이익", "당기순이익"}:
                row["thstrm_add_amount"] = amount
            rows.append(row)
        return httpx.Response(200, json={"status": "000", "list": rows})

    client = OpenDartFinancialClient(
        "x" * 40,
        corp_code_cache_path=corp_cache,
        transport=httpx.MockTransport(handler),
    )
    output = tmp_path / "latest.json"
    batch = await refresh_fundamental_snapshots(
        client,
        [("005930", "삼성전자")],
        output_path=output,
        as_of=date(2026, 7, 28),
    )
    snapshot = batch.snapshots[0]
    assert snapshot.symbol == "005930"
    assert snapshot.report_code == "11013"
    assert snapshot.debt_ratio_pct == Decimal("66.67")
    assert snapshot.current_ratio_pct == Decimal("250.00")
    assert snapshot.operating_margin_pct == Decimal("10.00")
    assert snapshot.health_status == "HEALTHY"
    assert output.exists()

    cached = await refresh_fundamental_snapshots(
        client,
        [("005930", "삼성전자")],
        output_path=output,
        as_of=date(2026, 7, 28),
    )
    assert cached.generated_at == batch.generated_at
