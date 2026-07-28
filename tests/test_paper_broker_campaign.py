from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from danta.adapters.kis.client import CashOrderReceipt, KisOrderStatus
from danta.config import AppSettings, KisCredentials, TradingEnvironment
from danta.ports.broker import Quote
from danta.services.paper_broker_campaign import PaperBrokerCampaign
from danta.services.policy_registry import load_policy_registry


class FakeCampaignBroker:
    def __init__(self, price: int = 221_278) -> None:
        self.price = price
        self.orders: list[dict[str, Any]] = []
        self.cancellations: list[dict[str, Any]] = []

    async def current_price(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, price=self.price, change_rate=None, raw_timestamp=None)

    async def submit_cash_order(self, **kwargs: Any) -> CashOrderReceipt:
        self.orders.append(kwargs)
        return CashOrderReceipt(
            broker_order_no=f"order-{len(self.orders)}",
            order_time="120000",
            branch_no="00000",
        )

    async def cancel_cash_order(self, **kwargs: Any) -> CashOrderReceipt:
        self.cancellations.append(kwargs)
        return CashOrderReceipt(
            broker_order_no="cancel-1",
            order_time="120100",
            branch_no="00000",
        )


def _campaign(tmp_path: Path) -> PaperBrokerCampaign:
    settings = AppSettings(paper_order_execution_enabled=True)
    credentials = KisCredentials(
        environment=TradingEnvironment.PAPER,
        app_key=SecretStr("paper-key"),
        app_secret=SecretStr("paper-secret"),
        account_no="12345678",
        product_code="01",
        hts_id=SecretStr("paper-user"),
    )
    return PaperBrokerCampaign(
        settings=settings,
        credentials=credentials,
        policies=load_policy_registry(Path("config/trading_policies.paper.json")),
        output=tmp_path / "campaign.jsonl",
    )


def _status(
    *,
    order_no: str,
    side: str,
    filled: int,
    remaining: int,
    price: int,
) -> KisOrderStatus:
    return KisOrderStatus(
        broker_order_no=order_no,
        original_order_no="",
        symbol="005930",
        side=side,
        ordered_quantity=1,
        filled_quantity=filled,
        remaining_quantity=remaining,
        order_price=price,
        average_fill_price=Decimal(price) if filled else Decimal("0"),
        order_time="120000",
        branch_no="00000",
    )


@pytest.mark.asyncio
async def test_campaign_step_round_trips_one_share_and_normalizes_tick(
    tmp_path: Path,
) -> None:
    campaign = _campaign(tmp_path)
    broker = FakeCampaignBroker()
    buy = _status(
        order_no="order-1",
        side="BUY",
        filled=1,
        remaining=0,
        price=221_000,
    )
    sell = _status(
        order_no="order-2",
        side="SELL",
        filled=1,
        remaining=0,
        price=221_500,
    )
    campaign._wait_for_terminal_or_timeout = AsyncMock(return_value=buy)  # type: ignore[method-assign]
    campaign._monitor_position = AsyncMock(  # type: ignore[method-assign]
        return_value=("CAMPAIGN_FORCED_EXIT", 17)
    )
    campaign._wait_for_fill = AsyncMock(return_value=sell)  # type: ignore[method-assign]

    result = await campaign._run_step(  # type: ignore[arg-type]
        broker,
        symbol="005930",
        discount=Decimal("0"),
        trading_date="20260728",
    )

    assert result.status == "ROUND_TRIP_FILLED"
    assert result.target_price == 221_000
    assert result.monitor_samples == 17
    assert broker.orders == [
        {
            "side": "BUY",
            "symbol": "005930",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 221_000,
        },
        {
            "side": "SELL",
            "symbol": "005930",
            "quantity": 1,
            "order_type": "MARKET",
        },
    ]


@pytest.mark.asyncio
async def test_campaign_step_cancels_unfilled_limit_order(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    broker = FakeCampaignBroker()
    open_order = _status(
        order_no="order-1",
        side="BUY",
        filled=0,
        remaining=1,
        price=220_000,
    )
    campaign._wait_for_terminal_or_timeout = AsyncMock(  # type: ignore[method-assign]
        return_value=open_order
    )
    campaign._wait_until_no_open_order = AsyncMock()  # type: ignore[method-assign]

    result = await campaign._run_step(  # type: ignore[arg-type]
        broker,
        symbol="005930",
        discount=Decimal("0.5"),
        trading_date="20260728",
    )

    assert result.status == "NOT_FILLED_CANCELLED"
    assert result.target_price == 220_000
    assert broker.cancellations == [
        {
            "broker_order_no": "order-1",
            "branch_no": "00000",
            "quantity": 1,
        }
    ]


def test_campaign_rejects_production_environment(tmp_path: Path) -> None:
    credentials = KisCredentials(
        environment=TradingEnvironment.PROD,
        app_key=SecretStr("prod-key"),
        app_secret=SecretStr("prod-secret"),
        account_no="12345678",
        product_code="01",
        hts_id=SecretStr("prod-user"),
    )
    with pytest.raises(PermissionError, match="paper-only"):
        PaperBrokerCampaign(
            settings=AppSettings(environment=TradingEnvironment.PROD),
            credentials=credentials,
            policies=load_policy_registry(Path("config/trading_policies.paper.json")),
            output=tmp_path / "campaign.jsonl",
        )
