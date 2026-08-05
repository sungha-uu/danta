from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from danta.services.operations_dashboard import build_operations_dashboard
from danta.services.public_performance import (
    PublicPerformanceReport,
    _load_realized_returns,
    build_public_performance_page,
    validate_public_performance,
)
from danta.services.system_health import OperationsHealthReport, SystemHealthRow

KST = ZoneInfo("Asia/Seoul")


def test_public_performance_page_is_utf8_and_sanitized(tmp_path: Path) -> None:
    report = PublicPerformanceReport(
        generated_at=datetime(2026, 8, 5, 15, 45, tzinfo=KST),
        initial_capital_amount=50_000_000,
        net_asset_amount=52_000_000,
        invested_amount=10_000_000,
        cash_ratio_pct=Decimal("80.77"),
        holdings_profit_loss_amount=500_000,
        cumulative_profit_loss_amount=2_000_000,
        cumulative_return_pct=Decimal("4.00"),
        today_buy_amount=1_000_000,
        today_sell_amount=0,
        holdings=[],
        recent_trades=[],
        daily_history=[],
    )
    path = build_public_performance_page(
        report,
        tmp_path,
        operations_url="https://example.test/operations/",
    )
    html = path.read_text(encoding="utf-8")
    assert "Danta 자율매매 실적" in html
    assert "최초 투자금" in html
    assert "trade-buy" in html
    assert "trade-sell" in html
    assert "x.side==='BUY'?'trade-buy':'trade-sell'" in html
    assert "매도수익률" in html
    assert "수수료·세금 전" in html
    assert "font-weight:400" in html
    assert "한국투자증권" not in html
    assert "account_no" not in html


def test_realized_return_is_reconstructed_from_position_generation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE fills (
                broker_order_id TEXT, price INTEGER, quantity INTEGER
            );
            CREATE TABLE broker_orders (id TEXT, order_intent_id TEXT);
            CREATE TABLE order_intents (
                id TEXT, created_at TEXT, symbol TEXT, side TEXT, generation INTEGER
            );
            CREATE TABLE positions (
                symbol TEXT, generation INTEGER, average_entry_price INTEGER
            );
            INSERT INTO positions VALUES ('000660', 2, 100000);
            INSERT INTO order_intents VALUES (
                'intent-1', '2026-08-05 00:30:00', '000660', 'SELL', 2
            );
            INSERT INTO broker_orders VALUES ('order-1', 'intent-1');
            INSERT INTO fills VALUES ('order-1', 110000, 3);
            """
        )

    returns = _load_realized_returns(
        f"sqlite+aiosqlite:///{database.as_posix()}"
    )

    assert returns[("2026-08-05", "000660", 3, 110000)] == Decimal("10.00")


def test_public_performance_rejects_sensitive_fields() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        validate_public_performance({"account_no": "12345678"})


def test_operations_dashboard_contains_six_numbered_systems(tmp_path: Path) -> None:
    rows = [
        SystemHealthRow(
            number=index,
            name=f"시스템 {index}",
            status="정상",
            current_work="운영 중",
            last_success="08-05 15:30:00",
            next_run="상시",
            issue="",
            dashboard_url="https://example.test/",
        )
        for index in range(1, 7)
    ]
    report = OperationsHealthReport(
        generated_at=datetime(2026, 8, 5, 15, 45, tzinfo=KST),
        normal_count=6,
        attention_count=0,
        rows=rows,
    )
    path = build_operations_dashboard(report, tmp_path)
    html = path.read_text(encoding="utf-8")
    assert "Danta 통합 운영 현황" in html
    assert html.count("시스템 ") == 6
    assert "<th>바로가기</th>" not in html
    assert html.count('href="https://example.test/"') == 4
