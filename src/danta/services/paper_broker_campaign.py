from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from danta.adapters.kis.client import KisClient, KisOrderStatus
from danta.adapters.kis.realtime import KisRealtimeClient
from danta.config import AppSettings, KisCredentials, TradingEnvironment
from danta.domain.market import MarketRisk
from danta.domain.price_tick import floor_kospi_price
from danta.domain.risk import PositionRiskSnapshot, evaluate_exit
from danta.services.market_signal import RollingMarketSignal
from danta.services.policy_registry import TradingPolicyRegistry

SEOUL = ZoneInfo("Asia/Seoul")
REGULAR_SESSION_END = time(15, 30)


@dataclass(frozen=True, slots=True)
class CampaignStepResult:
    symbol: str
    discount_pct: str
    reference_price: int
    target_price: int
    status: str
    buy_order_no: str | None
    sell_order_no: str | None
    buy_fill_price: str | None
    sell_fill_price: str | None
    monitor_samples: int
    exit_reason: str | None
    started_at: str
    completed_at: str


class PaperBrokerCampaign:
    """Paper-only one-share broker lifecycle campaign with forced flattening."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        credentials: KisCredentials,
        policies: TradingPolicyRegistry,
        output: Path,
        monitor_seconds: int = 30,
    ) -> None:
        if settings.environment is not TradingEnvironment.PAPER:
            raise PermissionError("campaign is paper-only")
        if credentials.environment is not TradingEnvironment.PAPER:
            raise PermissionError("campaign credentials must be paper")
        if not settings.paper_order_execution_enabled:
            raise PermissionError("paper order execution gate is closed")
        if monitor_seconds < 5:
            raise ValueError("campaign monitoring duration is too short")
        self.settings = settings
        self.credentials = credentials
        self.policies = policies
        self.output = output
        self.monitor_seconds = monitor_seconds
        self._session_closed_orders: set[str] = set()

    async def run(
        self,
        *,
        symbols: tuple[str, ...],
        discounts: tuple[Decimal, ...],
    ) -> list[CampaignStepResult]:
        if not symbols or not discounts:
            raise ValueError("symbols and discounts are required")
        results: list[CampaignStepResult] = []
        async with KisClient(
            self.credentials,
            token_cache_path=Path("data/kis-token-cache.json"),
            order_submission_enabled=True,
        ) as broker:
            if await broker.positions():
                raise RuntimeError("campaign requires an empty dedicated paper account")
            today = datetime.now().astimezone().strftime("%Y%m%d")
            open_orders = [
                status
                for status in await broker.daily_order_statuses(trading_date=today)
                if status.remaining_quantity > 0
            ]
            if open_orders:
                raise RuntimeError("campaign requires zero outstanding orders")
            for discount in discounts:
                for symbol in symbols:
                    result = await self._run_step(
                        broker, symbol=symbol, discount=discount, trading_date=today
                    )
                    results.append(result)
                    self._append(result)
                    if await broker.positions():
                        raise RuntimeError("campaign failed to flatten after a step")
                    if result.status == "NOT_FILLED_SESSION_CLOSED":
                        return results
        return results

    async def _run_step(
        self,
        broker: KisClient,
        *,
        symbol: str,
        discount: Decimal,
        trading_date: str,
    ) -> CampaignStepResult:
        started = datetime.now(UTC)
        quote = await broker.current_price(symbol)
        target = floor_kospi_price(
            int(Decimal(quote.price) * (Decimal("1") - discount / Decimal("100")))
        )
        receipt = await broker.submit_cash_order(
            side="BUY",
            symbol=symbol,
            quantity=1,
            order_type="LIMIT",
            limit_price=target,
        )
        status = await self._wait_for_terminal(
            broker,
            trading_date=trading_date,
            order_no=receipt.broker_order_no,
            symbol=symbol,
        )
        if status.filled_quantity == 0:
            return CampaignStepResult(
                symbol=symbol,
                discount_pct=str(discount),
                reference_price=quote.price,
                target_price=target,
                status=(
                    "NOT_FILLED_SESSION_CLOSED"
                    if receipt.broker_order_no in self._session_closed_orders
                    else "NOT_FILLED_CANCELLED_OR_EXPIRED"
                ),
                buy_order_no=receipt.broker_order_no,
                sell_order_no=None,
                buy_fill_price=None,
                sell_fill_price=None,
                monitor_samples=0,
                exit_reason=None,
                started_at=started.isoformat(),
                completed_at=datetime.now(UTC).isoformat(),
            )
        exit_reason, samples = await self._monitor_position(
            symbol=symbol,
            average_entry_price=status.average_fill_price,
        )
        sell = await broker.submit_cash_order(
            side="SELL",
            symbol=symbol,
            quantity=1,
            order_type="MARKET",
        )
        sell_status = await self._wait_for_fill(
            broker,
            trading_date=trading_date,
            order_no=sell.broker_order_no,
            symbol=symbol,
        )
        return CampaignStepResult(
            symbol=symbol,
            discount_pct=str(discount),
            reference_price=quote.price,
            target_price=target,
            status="ROUND_TRIP_FILLED",
            buy_order_no=receipt.broker_order_no,
            sell_order_no=sell.broker_order_no,
            buy_fill_price=str(status.average_fill_price),
            sell_fill_price=str(sell_status.average_fill_price),
            monitor_samples=samples,
            exit_reason=exit_reason,
            started_at=started.isoformat(),
            completed_at=datetime.now(UTC).isoformat(),
        )

    async def _monitor_position(
        self,
        *,
        symbol: str,
        average_entry_price: Decimal,
    ) -> tuple[str, int]:
        realtime = KisRealtimeClient(self.credentials)
        signal = RollingMarketSignal(symbol)
        peak = Decimal("0")
        samples = 0
        started = datetime.now(UTC)
        try:
            try:
                async with asyncio.timeout(self.monitor_seconds):
                    async for event in realtime.stream([symbol]):
                        signal.update(event)
                        if not signal.ready:
                            continue
                        now = datetime.now(UTC)
                        snapshot = signal.snapshot(
                            now=now,
                            market_risk=MarketRisk.NORMAL,
                            market_stress_score=Decimal("0"),
                            box_valid=True,
                            data_fresh=True,
                        )
                        current_return = (
                            (Decimal(snapshot.last_price) - average_entry_price)
                            / average_entry_price
                            * Decimal("100")
                        )
                        peak = max(peak, current_return)
                        samples += 1
                        decision = evaluate_exit(
                            PositionRiskSnapshot(
                                symbol=symbol,
                                generation=0,
                                average_entry_price=average_entry_price,
                                quantity=1,
                                sellable_quantity=1,
                                last_price=snapshot.last_price,
                                best_bid=snapshot.best_bid,
                                broker_return_pct=None,
                                peak_return_pct=peak,
                                held_minutes=int(
                                    (now - started).total_seconds() // 60
                                ),
                                sell_pressure_score=snapshot.sell_pressure_score,
                                weakness_score=snapshot.weakness_score,
                                market_stress_score=snapshot.market_stress_score,
                                market_risk=snapshot.market_risk,
                                box_valid=True,
                                data_fresh=snapshot.data_fresh,
                                observed_at=snapshot.observed_at,
                            ),
                            policy=self.policies.exit.to_domain(),
                        )
                        if decision.action.value == "SELL_MARKET":
                            return decision.reason_codes[0], samples
            except TimeoutError:
                pass
            return "CAMPAIGN_FORCED_EXIT", samples
        finally:
            await realtime.close()

    async def _wait_for_terminal(
        self,
        broker: KisClient,
        *,
        trading_date: str,
        order_no: str,
        symbol: str,
    ) -> KisOrderStatus:
        session_close_cancel_requested = False
        while True:
            statuses = await broker.daily_order_statuses(
                trading_date=trading_date,
                symbol=symbol,
                broker_order_no=order_no,
            )
            if statuses:
                latest = statuses[0]
                if latest.remaining_quantity == 0:
                    return latest
                if (
                    latest.side == "BUY"
                    and self._regular_session_closed()
                    and not session_close_cancel_requested
                ):
                    await broker.cancel_cash_order(
                        broker_order_no=latest.broker_order_no,
                        branch_no=latest.branch_no,
                        quantity=latest.remaining_quantity,
                    )
                    self._session_closed_orders.add(latest.broker_order_no)
                    session_close_cancel_requested = True
            await asyncio.sleep(2)

    @staticmethod
    def _regular_session_closed() -> bool:
        now = datetime.now(SEOUL)
        return now.weekday() < 5 and now.time() >= REGULAR_SESSION_END

    async def _wait_for_fill(
        self,
        broker: KisClient,
        *,
        trading_date: str,
        order_no: str,
        symbol: str,
    ) -> KisOrderStatus:
        status = await self._wait_for_terminal(
            broker,
            trading_date=trading_date,
            order_no=order_no,
            symbol=symbol,
        )
        if status.filled_quantity != 1:
            raise RuntimeError("forced paper sell was not fully filled")
        return status

    def _append(self, result: CampaignStepResult) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with self.output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
