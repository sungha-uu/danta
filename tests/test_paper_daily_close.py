from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from danta.adapters.kis.client import (
    KisAccountSnapshot,
    KisAccountSummary,
    KisDailyBar,
    KisOrderStatus,
)
from danta.config import (
    AppSettings,
    KisCredentials,
    SmtpConfig,
    TradingEnvironment,
)
from danta.ports.broker import AccountPosition, Quote
from danta.services.notifier import SmtpNotifier
from danta.services.paper_autonomous_campaign import (
    create_campaign_authorization,
    write_campaign_authorization,
)
from danta.services.paper_daily_close import (
    PaperDailyCloseError,
    run_paper_daily_close,
)

KST = ZoneInfo("Asia/Seoul")


class FakeCloseBroker:
    def __init__(self, *, trading_day: bool = True) -> None:
        self.trading_day = trading_day

    async def daily_bars(
        self,
        symbol: str,
        *,
        start_date: str,
        end_date: str,
    ) -> list[KisDailyBar]:
        assert symbol == "005930"
        assert start_date == end_date
        if not self.trading_day:
            return []
        return [
            KisDailyBar(
                trading_date=start_date,
                close=210_000,
                volume=100,
                trading_value=1_000,
            )
        ]

    async def daily_order_statuses(
        self,
        *,
        trading_date: str,
        symbol: str = "",
        broker_order_no: str = "",
    ) -> list[KisOrderStatus]:
        assert trading_date == "20260731"
        assert symbol == broker_order_no == ""
        return [
            KisOrderStatus(
                broker_order_no="1",
                original_order_no="",
                symbol="005930",
                side="BUY",
                ordered_quantity=2,
                filled_quantity=2,
                remaining_quantity=0,
                order_price=200_000,
                average_fill_price=Decimal("200000"),
                order_time="091500",
                branch_no="",
            ),
            KisOrderStatus(
                broker_order_no="2",
                original_order_no="",
                symbol="000660",
                side="BUY",
                ordered_quantity=1,
                filled_quantity=0,
                remaining_quantity=1,
                order_price=1_300_000,
                average_fill_price=Decimal("0"),
                order_time="150000",
                branch_no="",
            ),
        ]

    async def account_snapshot(self) -> KisAccountSnapshot:
        return KisAccountSnapshot(
            positions=(
                AccountPosition(
                    symbol="005930",
                    quantity=2,
                    sellable_quantity=2,
                    average_price=Decimal("200000"),
                ),
            ),
            summary=KisAccountSummary(
                cash_balance=1_000_000,
                securities_evaluation_amount=420_000,
                total_evaluation_amount=1_420_000,
                net_asset_amount=1_420_000,
                purchase_amount=400_000,
                holdings_evaluation_amount=420_000,
                holdings_profit_loss=20_000,
                asset_change_amount=10_000,
                asset_change_return_pct=Decimal("0.71"),
                today_buy_amount=400_000,
                today_sell_amount=0,
            ),
        )

    async def current_price(self, symbol: str) -> Quote:
        assert symbol == "005930"
        return Quote(
            symbol=symbol,
            price=210_000,
            change_rate=Decimal("1.0"),
            raw_timestamp="153000",
        )


class RecordingNotifier(SmtpNotifier):
    def __init__(self) -> None:
        super().__init__(
            SmtpConfig(
                smtp_server="smtp.example.com",
                smtp_port=465,
                use_ssl=True,
                sender="sender@example.com",
                password="secret",
                recipients=["receiver@example.com"],
            )
        )
        self.bodies: list[str] = []

    def send_daily_close(self, body: str):
        self.bodies.append(body)
        return super().send_daily_close(body)

    def _deliver(self, _message) -> None:
        return None


def _settings_and_credentials(
    tmp_path: Path,
    now: datetime,
) -> tuple[AppSettings, KisCredentials]:
    campaign_path = tmp_path / "private" / "campaign.json"
    write_campaign_authorization(
        create_campaign_authorization(now=now - timedelta(days=1), days=30),
        campaign_path,
    )
    settings = AppSettings(
        environment=TradingEnvironment.PAPER,
        paper_autonomous_campaign_path=campaign_path,
        paper_autonomous_kill_switch_path=tmp_path / "private" / "stop",
        paper_daily_close_root=tmp_path / "close",
        paper_autonomous_report_path=tmp_path / "missing-report.json",
        smtp_enabled=True,
    )
    credentials = KisCredentials(
        environment=TradingEnvironment.PAPER,
        app_key="key",
        app_secret="secret",
        account_no="12345678",
        product_code="01",
        hts_id="user",
    )
    return settings, credentials


@pytest.mark.asyncio
async def test_paper_daily_close_sends_once_and_persists_summary(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 31, 15, 35, tzinfo=KST)
    settings, credentials = _settings_and_credentials(tmp_path, now)
    notifier = RecordingNotifier()
    broker = FakeCloseBroker()

    first = await run_paper_daily_close(
        settings,
        credentials,
        broker,
        notifier,
        now=now,
    )
    second = await run_paper_daily_close(
        settings,
        credentials,
        broker,
        notifier,
        now=now,
    )

    assert first.status == "SENT"
    assert first.recipient_count == 1
    assert second.status == "ALREADY_SENT"
    assert len(notifier.bodies) == 1
    assert "[당일 매수 체결]" in notifier.bodies[0]
    assert "[현재 보유종목]" in notifier.bodies[0]
    assert "자율매매 최초 원금: 50,000,000원" in notifier.bodies[0]
    assert "자율매매 누적손익: -48,580,000원" in notifier.bodies[0]
    assert "자율매매 누적수익률: -97.16%" in notifier.bodies[0]
    assert Path(first.report_path or "").exists()


@pytest.mark.asyncio
async def test_paper_daily_close_skips_non_trading_day(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 31, 15, 35, tzinfo=KST)
    settings, credentials = _settings_and_credentials(tmp_path, now)
    notifier = RecordingNotifier()
    result = await run_paper_daily_close(
        settings,
        credentials,
        FakeCloseBroker(trading_day=False),
        notifier,
        now=now,
    )
    assert result.status == "NON_TRADING_DAY"
    assert notifier.bodies == []


@pytest.mark.asyncio
async def test_paper_daily_close_rejects_early_execution(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 31, 15, 20, tzinfo=KST)
    settings, credentials = _settings_and_credentials(tmp_path, now)
    with pytest.raises(PaperDailyCloseError, match="before 15:30"):
        await run_paper_daily_close(
            settings,
            credentials,
            FakeCloseBroker(),
            RecordingNotifier(),
            now=now,
        )
