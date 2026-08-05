# ruff: noqa: E501
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from functools import partial
from html import escape
from pathlib import Path
from typing import Literal, Protocol, TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from danta.adapters.kis.client import KisAccountSnapshot, KisApiError, KisOrderStatus
from danta.config import AppSettings
from danta.ports.broker import Quote

KST = ZoneInfo("Asia/Seoul")
HUNDRED = Decimal("100")
T = TypeVar("T")


class PublicPerformanceBroker(Protocol):
    async def account_snapshot(self) -> KisAccountSnapshot: ...

    async def current_price(self, symbol: str) -> Quote: ...

    async def daily_order_statuses(
        self,
        *,
        trading_date: str,
        symbol: str = "",
        broker_order_no: str = "",
    ) -> list[KisOrderStatus]: ...


class PublicHolding(BaseModel):
    symbol: str
    name: str
    quantity: int = Field(gt=0)
    average_price: int = Field(gt=0)
    current_price: int = Field(gt=0)
    evaluation_amount: int = Field(ge=0)
    profit_loss_amount: int
    return_pct: Decimal


class PublicTrade(BaseModel):
    trading_date: str
    order_time: str
    symbol: str
    name: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    average_fill_price: int = Field(gt=0)


class PublicDailyPerformance(BaseModel):
    trading_date: str
    net_asset_amount: int = Field(ge=0)
    cumulative_profit_loss_amount: int
    cumulative_return_pct: Decimal


class PublicPerformanceReport(BaseModel):
    schema_version: Literal["danta-public-performance-v1"] = (
        "danta-public-performance-v1"
    )
    generated_at: datetime
    delayed_minutes: int = 15
    initial_capital_amount: int = Field(gt=0)
    net_asset_amount: int = Field(ge=0)
    invested_amount: int = Field(ge=0)
    cash_ratio_pct: Decimal
    holdings_profit_loss_amount: int
    cumulative_profit_loss_amount: int
    cumulative_return_pct: Decimal
    today_buy_amount: int = Field(ge=0)
    today_sell_amount: int = Field(ge=0)
    holdings: list[PublicHolding]
    recent_trades: list[PublicTrade]
    daily_history: list[PublicDailyPerformance]


async def collect_public_performance(
    settings: AppSettings,
    broker: PublicPerformanceBroker,
    *,
    now: datetime | None = None,
) -> PublicPerformanceReport:
    current = (now or datetime.now(KST)).astimezone(KST)
    names = _load_names(settings.paper_autonomous_report_path)
    account = await _kis_read(lambda: broker.account_snapshot())
    holdings: list[PublicHolding] = []
    for position in account.positions:
        symbol = position.symbol
        quote = await _kis_read(partial(broker.current_price, symbol))
        cost = int(position.average_price) * position.quantity
        evaluation = quote.price * position.quantity
        profit_loss = evaluation - cost
        return_pct = (
            Decimal(profit_loss) / Decimal(cost) * HUNDRED
            if cost > 0
            else Decimal("0")
        )
        holdings.append(
            PublicHolding(
                symbol=position.symbol,
                name=names.get(position.symbol, position.symbol),
                quantity=position.quantity,
                average_price=int(position.average_price),
                current_price=quote.price,
                evaluation_amount=evaluation,
                profit_loss_amount=profit_loss,
                return_pct=_pct(return_pct),
            )
        )
    statuses = await _kis_read(
        lambda: broker.daily_order_statuses(
            trading_date=current.strftime("%Y%m%d")
        )
    )
    current_trades = [
        PublicTrade(
            trading_date=current.date().isoformat(),
            order_time=status.order_time,
            symbol=status.symbol,
            name=names.get(status.symbol, status.symbol),
            side=status.side,  # type: ignore[arg-type]
            quantity=status.filled_quantity,
            average_fill_price=int(status.average_fill_price),
        )
        for status in statuses
        if status.side in {"BUY", "SELL"}
        and status.filled_quantity > 0
        and status.average_fill_price > 0
    ]
    historical_trades, history = _load_public_history(
        settings.paper_daily_close_root / "reports",
        names,
    )
    trades = _deduplicate_trades([*current_trades, *historical_trades])[:100]
    summary = account.summary
    baseline = settings.autonomous_initial_capital_krw
    cumulative = summary.net_asset_amount - baseline
    cumulative_pct = Decimal(cumulative) / Decimal(baseline) * HUNDRED
    cash_ratio = (
        Decimal(summary.cash_balance) / Decimal(summary.net_asset_amount) * HUNDRED
        if summary.net_asset_amount > 0
        else Decimal("0")
    )
    report = PublicPerformanceReport(
        generated_at=current,
        initial_capital_amount=baseline,
        net_asset_amount=summary.net_asset_amount,
        invested_amount=summary.purchase_amount,
        cash_ratio_pct=_pct(cash_ratio),
        holdings_profit_loss_amount=summary.holdings_profit_loss,
        cumulative_profit_loss_amount=cumulative,
        cumulative_return_pct=_pct(cumulative_pct),
        today_buy_amount=summary.today_buy_amount,
        today_sell_amount=summary.today_sell_amount,
        holdings=holdings,
        recent_trades=trades,
        daily_history=history[-30:],
    )
    validate_public_performance(report.model_dump(mode="json"))
    return report


def build_public_performance_page(
    report: PublicPerformanceReport,
    output_dir: Path,
    *,
    operations_url: str,
) -> Path:
    payload = report.model_dump(mode="json")
    validate_public_performance(payload)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = _performance_html(encoded, report, operations_url=operations_url)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "index.html"
    temporary = output_dir / ".index.html.tmp"
    temporary.write_text(html, encoding="utf-8")
    temporary.replace(target)
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    return target


def validate_public_performance(payload: object) -> None:
    forbidden_keys = {
        "account_no",
        "account_number",
        "account_alias",
        "broker_order_no",
        "approval_id",
        "command_id",
        "orderable_cash",
        "app_key",
        "app_secret",
        "hts_id",
        "email",
        "sender",
        "recipients",
    }

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in forbidden_keys:
                    raise ValueError(f"forbidden public performance field: {key}")
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            lowered = value.lower()
            if "한국투자" in value or "koreainvestment" in lowered:
                raise ValueError("broker identity is forbidden in public performance")
            if re.search(r"(?<!\d)\d{8}-\d{2}(?!\d)", value):
                raise ValueError("account-like identifier found in public performance")

    walk(payload)


def _load_public_history(
    root: Path,
    names: dict[str, str],
) -> tuple[list[PublicTrade], list[PublicDailyPerformance]]:
    trades: list[PublicTrade] = []
    history: list[PublicDailyPerformance] = []
    for path in sorted(root.glob("*.json"), reverse=True)[:30]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        date = str(raw.get("trading_date", path.stem))
        history.append(
            PublicDailyPerformance(
                trading_date=date,
                net_asset_amount=max(0, int(raw.get("net_asset_amount", 0))),
                cumulative_profit_loss_amount=int(
                    raw.get("cumulative_profit_loss_amount", 0)
                ),
                cumulative_return_pct=Decimal(
                    str(raw.get("cumulative_return_pct", "0"))
                ),
            )
        )
        for fill in raw.get("fills", []):
            if not isinstance(fill, dict) or fill.get("side") not in {"BUY", "SELL"}:
                continue
            quantity = int(fill.get("quantity", 0))
            price = int(Decimal(str(fill.get("average_fill_price", 0))))
            if quantity <= 0 or price <= 0:
                continue
            symbol = str(fill.get("symbol", ""))
            trades.append(
                PublicTrade(
                    trading_date=date,
                    order_time=str(fill.get("order_time", "")),
                    symbol=symbol,
                    name=names.get(symbol, str(fill.get("name", symbol))),
                    side=str(fill["side"]),  # type: ignore[arg-type]
                    quantity=quantity,
                    average_fill_price=price,
                )
            )
    history.sort(key=lambda item: item.trading_date)
    return trades, history


def _deduplicate_trades(items: list[PublicTrade]) -> list[PublicTrade]:
    found: dict[tuple[object, ...], PublicTrade] = {}
    for item in items:
        key = (
            item.trading_date,
            item.order_time,
            item.symbol,
            item.side,
            item.quantity,
            item.average_fill_price,
        )
        found[key] = item
    return sorted(
        found.values(),
        key=lambda item: (item.trading_date, item.order_time),
        reverse=True,
    )


def _load_names(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    names: dict[str, str] = {}
    for key in ("candidates", "extended_watchlist"):
        for item in raw.get(key, []):
            if isinstance(item, dict) and item.get("code") and item.get("name"):
                names[str(item["code"])] = str(item["name"])
    return names


def _pct(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


async def _kis_read(operation: Callable[[], Awaitable[T]]) -> T:
    """Retry read-only snapshots across the unified runtime's KIS rate slots."""
    for attempt in range(8):
        try:
            return await operation()
        except KisApiError as exc:
            if "EGW00201" not in str(exc) or attempt == 7:
                raise
            await asyncio.sleep(1.1 + attempt * 0.25)
    raise RuntimeError("unreachable KIS retry state")


def _performance_html(
    encoded: str,
    report: PublicPerformanceReport,
    *,
    operations_url: str,
) -> str:
    generated = escape(report.generated_at.astimezone(KST).strftime("%Y-%m-%d %H:%M"))
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Danta 자율매매 실적</title><style>
:root{{--navy:#111b2b;--blue:#2d477b;--bg:#f3f6fa;--line:#d9e0ea;--red:#b9433b}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:#152036;font-family:Arial,'Malgun Gothic',sans-serif}}header{{background:var(--navy);color:#fff;padding:22px 28px;display:flex;justify-content:space-between;align-items:center}}header h1{{margin:0;font-size:28px}}a{{color:inherit}}main{{padding:22px;max-width:1600px;margin:auto}}.note{{color:#647087;font-size:13px}}.kpis{{display:grid;grid-template-columns:repeat(6,minmax(150px,1fr));gap:12px;margin:18px 0}}.card{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px;box-shadow:0 2px 8px #1720330b}}.label{{color:#68748b;font-size:13px}}.value{{font-size:24px;font-weight:800;margin-top:8px}}.positive{{color:var(--red)}}.negative{{color:#3568c5}}.trade-buy{{color:#b9433b;font-weight:700}}.trade-sell{{color:#3568c5;font-weight:700}}h2{{margin:28px 0 12px}}table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line)}}th{{background:#2c3f70;color:#fff;padding:12px}}td{{padding:11px;border-bottom:1px solid var(--line);text-align:center}}tbody tr:nth-child(even){{background:#f8f9fc}}@media(max-width:900px){{.kpis{{grid-template-columns:repeat(2,1fr)}}main{{padding:12px}}table{{font-size:12px}}}}</style></head><body><header><h1>Danta 자율매매 실적</h1><a href="{escape(operations_url)}">통합 운영 현황으로</a></header><main><div class="note">공개용 15분 지연 요약 · 계좌번호·증권사·주문번호·승인정보 미포함 · 기준 {generated}</div><section class="kpis" id="kpis"></section><h2>현재 보유종목</h2><div id="holdings"></div><h2>최근 매수·매도</h2><div id="trades"></div><h2>일별 누적 성과</h2><div id="history"></div></main><script>const D={encoded};const won=n=>Number(n).toLocaleString('ko-KR')+'원';const pct=n=>(Number(n)<0?'-':'')+Math.abs(Number(n)).toFixed(2)+'%';const cls=n=>Number(n)<0?'negative':Number(n)>0?'positive':'';const KP=[['최초 투자금',won(D.initial_capital_amount)],['현재 순자산',won(D.net_asset_amount)],['현재 투자금',won(D.invested_amount)],['누적손익',won(D.cumulative_profit_loss_amount)],['누적수익률',pct(D.cumulative_return_pct)],['현금 비율',pct(D.cash_ratio_pct)]];document.querySelector('#kpis').innerHTML=KP.map((x,i)=>`<div class="card"><div class="label">${{x[0]}}</div><div class="value ${{i===3||i===4?cls(i===3?D.cumulative_profit_loss_amount:D.cumulative_return_pct):''}}">${{x[1]}}</div></div>`).join('');const table=(heads,rows)=>`<table><thead><tr>${{heads.map(x=>`<th>${{x}}</th>`).join('')}}</tr></thead><tbody>${{rows.join('')||`<tr><td colspan="${{heads.length}}">내역 없음</td></tr>`}}</tbody></table>`;document.querySelector('#holdings').innerHTML=table(['종목','수량','평균가','현재가','평가금액','평가손익','수익률'],D.holdings.map(x=>`<tr><td>${{x.name}} (${{x.symbol}})</td><td>${{x.quantity.toLocaleString()}}</td><td>${{won(x.average_price)}}</td><td>${{won(x.current_price)}}</td><td>${{won(x.evaluation_amount)}}</td><td class="${{cls(x.profit_loss_amount)}}">${{won(x.profit_loss_amount)}}</td><td class="${{cls(x.return_pct)}}">${{pct(x.return_pct)}}</td></tr>`));document.querySelector('#trades').innerHTML=table(['일자','시간','종목','구분','수량','체결가'],D.recent_trades.map(x=>`<tr><td>${{x.trading_date}}</td><td>${{x.order_time}}</td><td>${{x.name}} (${{x.symbol}})</td><td class="${{x.side==='BUY'?'trade-buy':'trade-sell'}}">${{x.side==='BUY'?'매수':'매도'}}</td><td class="${{x.side==='BUY'?'trade-buy':'trade-sell'}}">${{x.quantity.toLocaleString()}}</td><td class="${{x.side==='BUY'?'trade-buy':'trade-sell'}}">${{won(x.average_fill_price)}}</td></tr>`));document.querySelector('#history').innerHTML=table(['일자','순자산','누적손익','누적수익률'],[...D.daily_history].reverse().map(x=>`<tr><td>${{x.trading_date}}</td><td>${{won(x.net_asset_amount)}}</td><td class="${{cls(x.cumulative_profit_loss_amount)}}">${{won(x.cumulative_profit_loss_amount)}}</td><td class="${{cls(x.cumulative_return_pct)}}">${{pct(x.cumulative_return_pct)}}</td></tr>`));</script></body></html>"""
