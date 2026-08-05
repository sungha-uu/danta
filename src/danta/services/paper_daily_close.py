from __future__ import annotations

import json
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from danta.adapters.kis.client import (
    KisAccountSnapshot,
    KisApiError,
    KisDailyBar,
    KisOrderStatus,
)
from danta.config import (
    AppSettings,
    KisCredentials,
)
from danta.dashboard.builder import load_dashboard_report
from danta.ports.broker import Quote
from danta.services.autonomous_campaign import load_campaign_authorization
from danta.services.notifier import SmtpNotifier
from danta.services.runtime_repository import StoredPosition

KST = ZoneInfo("Asia/Seoul")
HUNDRED = Decimal("100")


class PaperDailyCloseError(RuntimeError):
    """Raised when a paper-account close report cannot be trusted."""


class PaperDailyCloseBroker(Protocol):
    async def daily_bars(
        self,
        symbol: str,
        *,
        start_date: str,
        end_date: str,
    ) -> list[KisDailyBar]: ...

    async def daily_order_statuses(
        self,
        *,
        trading_date: str,
        symbol: str = "",
        broker_order_no: str = "",
    ) -> list[KisOrderStatus]: ...

    async def account_snapshot(self) -> KisAccountSnapshot: ...

    async def current_price(self, symbol: str) -> Quote: ...


class PaperDailyFill(BaseModel):
    symbol: str
    name: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    average_fill_price: Decimal = Field(gt=0)
    order_time: str
    broker_order_no: str


class PaperDailyOpenOrder(BaseModel):
    symbol: str
    name: str
    side: Literal["BUY", "SELL"]
    remaining_quantity: int = Field(gt=0)
    order_price: int = Field(ge=0)
    order_time: str


class PaperDailyHolding(BaseModel):
    symbol: str
    name: str
    quantity: int = Field(gt=0)
    sellable_quantity: int = Field(ge=0)
    average_price: Decimal = Field(gt=0)
    current_price: int = Field(gt=0)
    evaluation_amount: int = Field(ge=0)
    profit_loss_amount: Decimal
    return_pct: Decimal


class PaperDailyCloseReport(BaseModel):
    schema_version: Literal["daily-close-v3"] = "daily-close-v3"
    campaign_id: str
    trading_date: str
    generated_at: datetime
    environment: Literal["paper", "prod"]
    fills: list[PaperDailyFill]
    open_orders: list[PaperDailyOpenOrder]
    holdings: list[PaperDailyHolding]
    cash_balance: int
    purchase_amount: int
    holdings_evaluation_amount: int
    holdings_profit_loss: int
    holdings_return_pct: Decimal
    net_asset_amount: int
    asset_change_amount: int
    asset_change_return_pct: Decimal
    today_buy_amount: int
    today_sell_amount: int
    initial_capital_amount: int
    cumulative_profit_loss_amount: int
    cumulative_return_pct: Decimal
    reconciliation_status: Literal["MATCHED", "MISMATCH", "NOT_CHECKED"]
    reconciliation_detail: str


class PaperDailyCloseResult(BaseModel):
    status: Literal["SENT", "ALREADY_SENT", "NON_TRADING_DAY", "DISABLED"]
    trading_date: str
    report_path: str | None = None
    recipient_count: int = 0
    detail: str


async def run_paper_daily_close(
    settings: AppSettings,
    credentials: KisCredentials,
    broker: PaperDailyCloseBroker,
    notifier: SmtpNotifier,
    *,
    internal_positions: list[StoredPosition] | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> PaperDailyCloseResult:
    current = (now or datetime.now(KST)).astimezone(KST)
    trading_date = current.date().isoformat()
    if not settings.daily_close_enabled:
        return PaperDailyCloseResult(
            status="DISABLED",
            trading_date=trading_date,
            detail="daily close email is disabled",
        )
    if settings.environment is not credentials.environment:
        raise PermissionError("daily close environment mismatch")
    authorization = load_campaign_authorization(settings, credentials)
    if authorization is None:
        return PaperDailyCloseResult(
            status="DISABLED",
            trading_date=trading_date,
            detail="autonomous campaign is not authorized",
        )
    if current.weekday() >= 5 and not force:
        return PaperDailyCloseResult(
            status="NON_TRADING_DAY",
            trading_date=trading_date,
            detail="weekend close report skipped",
        )
    if current.timetz().replace(tzinfo=None) < time(15, 30) and not force:
        raise PaperDailyCloseError("daily close cannot run before 15:30 KST")

    root = settings.daily_close_root
    marker = root / "sent" / f"{trading_date}.json"
    if marker.exists() and not force:
        return PaperDailyCloseResult(
            status="ALREADY_SENT",
            trading_date=trading_date,
            report_path=str(root / "reports" / f"{trading_date}.json"),
            detail="daily close email was already sent",
        )
    if not force and not await _is_trading_day(broker, trading_date):
        return PaperDailyCloseResult(
            status="NON_TRADING_DAY",
            trading_date=trading_date,
            detail="KIS did not return a regular-session bar for this date",
        )

    names = _load_names(settings.autonomous_report_path)
    statuses = await broker.daily_order_statuses(trading_date=trading_date.replace("-", ""))
    account = await broker.account_snapshot()
    holdings: list[PaperDailyHolding] = []
    for position in account.positions:
        quote = await broker.current_price(position.symbol)
        evaluation = quote.price * position.quantity
        cost = position.average_price * Decimal(position.quantity)
        profit_loss = Decimal(evaluation) - cost
        return_pct = profit_loss / cost * HUNDRED if cost > 0 else Decimal("0")
        holdings.append(
            PaperDailyHolding(
                symbol=position.symbol,
                name=names.get(position.symbol, position.symbol),
                quantity=position.quantity,
                sellable_quantity=position.sellable_quantity,
                average_price=position.average_price,
                current_price=quote.price,
                evaluation_amount=evaluation,
                profit_loss_amount=_money(profit_loss),
                return_pct=_pct(return_pct),
            )
        )
    fills = [
        PaperDailyFill(
            symbol=status.symbol,
            name=names.get(status.symbol, status.symbol),
            side=status.side,  # type: ignore[arg-type]
            quantity=status.filled_quantity,
            average_fill_price=status.average_fill_price,
            order_time=status.order_time,
            broker_order_no=status.broker_order_no,
        )
        for status in statuses
        if status.filled_quantity > 0
        and status.average_fill_price > 0
        and status.side in {"BUY", "SELL"}
    ]
    open_orders = [
        PaperDailyOpenOrder(
            symbol=status.symbol,
            name=names.get(status.symbol, status.symbol),
            side=status.side,  # type: ignore[arg-type]
            remaining_quantity=status.remaining_quantity,
            order_price=status.order_price,
            order_time=status.order_time,
        )
        for status in statuses
        if status.remaining_quantity > 0 and status.side in {"BUY", "SELL"}
    ]
    summary = account.summary
    holdings_return = (
        Decimal(summary.holdings_profit_loss) / Decimal(summary.purchase_amount) * HUNDRED
        if summary.purchase_amount > 0
        else Decimal("0")
    )
    initial_capital = settings.autonomous_initial_capital_krw
    cumulative = (
        Decimal(summary.net_asset_amount) / Decimal(initial_capital) - Decimal("1")
    ) * HUNDRED
    reconciliation_status, reconciliation_detail = _reconcile(
        account,
        internal_positions,
    )
    report = PaperDailyCloseReport(
        campaign_id=authorization.campaign_id,
        trading_date=trading_date,
        generated_at=current,
        environment=settings.environment.value,
        fills=fills,
        open_orders=open_orders,
        holdings=holdings,
        cash_balance=summary.cash_balance,
        purchase_amount=summary.purchase_amount,
        holdings_evaluation_amount=summary.holdings_evaluation_amount,
        holdings_profit_loss=summary.holdings_profit_loss,
        holdings_return_pct=_pct(holdings_return),
        net_asset_amount=summary.net_asset_amount,
        asset_change_amount=summary.asset_change_amount,
        asset_change_return_pct=_pct(summary.asset_change_return_pct),
        today_buy_amount=summary.today_buy_amount,
        today_sell_amount=summary.today_sell_amount,
        initial_capital_amount=initial_capital,
        cumulative_profit_loss_amount=summary.net_asset_amount - initial_capital,
        cumulative_return_pct=_pct(cumulative),
        reconciliation_status=reconciliation_status,
        reconciliation_detail=reconciliation_detail,
    )
    report_path = root / "reports" / f"{trading_date}.json"
    _write_model(report_path, report)
    _write_model(root / "latest.json", report)
    receipt = notifier.send_daily_close(format_paper_daily_close(report))
    _write_json(
        marker,
        {
            "schema_version": 1,
            "trading_date": trading_date,
            "sent_at": datetime.now(UTC).isoformat(),
            "recipient_count": receipt.recipient_count,
            "report_path": str(report_path),
        },
    )
    return PaperDailyCloseResult(
        status="SENT",
        trading_date=trading_date,
        report_path=str(report_path),
        recipient_count=receipt.recipient_count,
        detail="autonomous daily close email sent",
    )


def format_paper_daily_close(report: PaperDailyCloseReport) -> str:
    lines = [
        f"기준일시: {report.generated_at.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S')}",
        f"모드: KIS {'실계좌' if report.environment == 'prod' else '모의투자'} 전자동",
        "",
        "[당일 매수 체결]",
    ]
    buys = [item for item in report.fills if item.side == "BUY"]
    sells = [item for item in report.fills if item.side == "SELL"]
    lines.extend(_fill_lines(buys) if buys else ["없음"])
    lines.extend(["", "[당일 매도 체결]"])
    lines.extend(_fill_lines(sells) if sells else ["없음"])
    lines.extend(["", "[미체결 주문]"])
    if report.open_orders:
        lines.extend(
            f"- {item.name} {item.side} {item.remaining_quantity:,}주 {item.order_price:,}원"
            for item in report.open_orders
        )
    else:
        lines.append("없음")
    lines.extend(["", "[현재 보유종목]"])
    if report.holdings:
        lines.extend(
            f"- {item.name} {item.quantity:,}주 | 평균 {item.average_price:,.0f}원 "
            f"| 현재 {item.current_price:,}원 | 평가손익 "
            f"{item.profit_loss_amount:+,.0f}원 ({item.return_pct:+.2f}%)"
            for item in report.holdings
        )
    else:
        lines.append("없음")
    lines.extend(
        [
            "",
            "[계좌 요약]",
            f"예수금: {report.cash_balance:,}원",
            f"보유주식 매입금액: {report.purchase_amount:,}원",
            f"보유주식 평가금액: {report.holdings_evaluation_amount:,}원",
            f"보유주식 평가손익: {report.holdings_profit_loss:+,}원 "
            f"({report.holdings_return_pct:+.2f}%)",
            f"계좌 순자산: {report.net_asset_amount:,}원",
            f"당일 자산증감: {report.asset_change_amount:+,}원 "
            f"({report.asset_change_return_pct:+.2f}%)",
            f"자율매매 최초 원금: {report.initial_capital_amount:,}원",
            f"자율매매 누적손익: {report.cumulative_profit_loss_amount:+,}원",
            f"자율매매 누적수익률: {report.cumulative_return_pct:+.2f}%",
            "",
            f"[잔고 대조] {report.reconciliation_status}",
            report.reconciliation_detail,
        ]
    )
    return "\n".join(lines)


async def _is_trading_day(
    broker: PaperDailyCloseBroker,
    trading_date: str,
) -> bool:
    compact = trading_date.replace("-", "")
    try:
        bars = await broker.daily_bars(
            "005930",
            start_date=compact,
            end_date=compact,
        )
    except KisApiError as exc:
        if "contained no price bars" in str(exc):
            return False
        raise
    return any(item.trading_date == compact for item in bars)


def _load_names(report_path: Path) -> dict[str, str]:
    if not report_path.exists():
        return {}
    report = load_dashboard_report(report_path)
    return {item.code: item.name for item in [*report.candidates, *report.extended_watchlist]}


def _reconcile(
    account: KisAccountSnapshot,
    internal_positions: list[StoredPosition] | None,
) -> tuple[Literal["MATCHED", "MISMATCH", "NOT_CHECKED"], str]:
    if internal_positions is None:
        return "NOT_CHECKED", "내부 포지션 저장소를 조회하지 않았습니다."
    broker = {item.symbol: item.quantity for item in account.positions}
    internal = {item.symbol: item.quantity for item in internal_positions}
    if broker == internal:
        return "MATCHED", "KIS 보유수량과 내부 OPEN 포지션이 일치합니다."
    return (
        "MISMATCH",
        f"KIS {broker} / 내부 {internal}; 신규매수 차단 및 복구 대조가 필요합니다.",
    )


def _fill_lines(items: list[PaperDailyFill]) -> list[str]:
    return [
        f"- {item.name} {item.quantity:,}주 {item.average_fill_price:,.0f}원 ({item.order_time})"
        for item in items
    ]


def _pct(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"))


def _write_model(path: Path, model: BaseModel) -> None:
    _write_json(path, model.model_dump(mode="json"))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
