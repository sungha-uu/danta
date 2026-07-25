from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any


class KrxDataError(RuntimeError):
    """Raised when KRX returns incomplete or structurally invalid market data."""


@dataclass(frozen=True, slots=True)
class DailyBar:
    trading_date: date
    close: Decimal
    volume: Decimal
    trading_value: Decimal


@dataclass(frozen=True, slots=True)
class MarketDataset:
    bars: dict[str, list[DailyBar]]
    names: dict[str, str]
    flows: dict[int, dict[str, dict[str, Decimal]]]
    trading_dates: list[date]
    market_caps: dict[str, Decimal] = field(default_factory=dict)


class PykrxMarketDataClient:
    INVESTORS = {
        "retail": "\uac1c\uc778",
        "foreign": "\uc678\uad6d\uc778",
        "institution": "\uae30\uad00\ud569\uacc4",
        "financial_investment": "\uae08\uc735\ud22c\uc790",
        "pension": "\uc5f0\uae30\uae08",
    }

    def __init__(self, *, minimum_universe_size: int = 300) -> None:
        self.minimum_universe_size = minimum_universe_size

    def collect(self, *, as_of: date | None = None, required_days: int = 42) -> MarketDataset:
        stock = self._stock_module()
        end = as_of or datetime.now().date()
        start = end - timedelta(days=required_days * 2 + 30)
        reference = stock.get_market_ohlcv_by_date(
            start.strftime("%Y%m%d"),
            end.strftime("%Y%m%d"),
            "005930",
        )
        if reference is None or len(reference.index) < required_days:
            raise KrxDataError("KRX did not return enough reference trading dates")
        trading_dates = [item.date() for item in reference.index][-required_days:]
        bars: dict[str, list[DailyBar]] = {}
        for trading_date in trading_dates:
            frame = stock.get_market_ohlcv_by_ticker(
                trading_date.strftime("%Y%m%d"),
                market="KOSPI",
            )
            if frame is None or len(frame.index) < self.minimum_universe_size:
                raise KrxDataError(
                    f"KRX KOSPI daily snapshot is incomplete: {trading_date.isoformat()}"
                )
            for symbol, row in frame.iterrows():
                values = list(row.values)
                if len(values) < 6:
                    raise KrxDataError("KRX OHLCV response has fewer than six columns")
                close = self._decimal(values[3])
                if close <= 0:
                    continue
                bars.setdefault(str(symbol), []).append(
                    DailyBar(
                        trading_date=trading_date,
                        close=close,
                        volume=self._decimal(values[4]),
                        trading_value=self._decimal(values[5]),
                    )
                )
        latest_symbols = {
            symbol
            for symbol, symbol_bars in bars.items()
            if symbol_bars[-1].trading_date == trading_dates[-1]
        }
        bars = {symbol: bars[symbol] for symbol in latest_symbols}
        flows, names = self._collect_flows(stock, trading_dates)
        market_caps = self._collect_market_caps(stock, trading_dates[-1])
        for symbol in latest_symbols:
            names.setdefault(symbol, str(stock.get_market_ticker_name(symbol)))
        return MarketDataset(
            bars=bars,
            names=names,
            flows=flows,
            trading_dates=trading_dates,
            market_caps=market_caps,
        )

    def _collect_market_caps(self, stock: Any, trading_date: date) -> dict[str, Decimal]:
        frame = stock.get_market_cap_by_ticker(
            trading_date.strftime("%Y%m%d"),
            market="KOSPI",
        )
        if frame is None or len(frame.index) < self.minimum_universe_size:
            raise KrxDataError(
                f"KRX KOSPI market-cap snapshot is incomplete: {trading_date.isoformat()}"
            )
        result: dict[str, Decimal] = {}
        for symbol, row in frame.iterrows():
            values = list(row.values)
            if len(values) < 2:
                raise KrxDataError("KRX market-cap response has fewer than two columns")
            result[str(symbol)] = self._decimal(values[1])
        return result

    def _collect_flows(
        self,
        stock: Any,
        trading_dates: list[date],
    ) -> tuple[dict[int, dict[str, dict[str, Decimal]]], dict[str, str]]:
        all_flows: dict[int, dict[str, dict[str, Decimal]]] = {}
        names: dict[str, str] = {}
        end = trading_dates[-1].strftime("%Y%m%d")
        for days in (7, 14, 21):
            start = trading_dates[-days].strftime("%Y%m%d")
            period: dict[str, dict[str, Decimal]] = {}
            successful_groups = 0
            for key, investor in self.INVESTORS.items():
                frame = stock.get_market_net_purchases_of_equities_by_ticker(
                    start,
                    end,
                    "KOSPI",
                    investor,
                )
                if frame is None or frame.empty or len(frame.columns) < 7:
                    continue
                successful_groups += 1
                for symbol, row in frame.iterrows():
                    values = list(row.values)
                    names.setdefault(str(symbol), str(values[0]))
                    period.setdefault(str(symbol), {})[key] = self._decimal(values[6]) / Decimal(
                        "100000000"
                    )
            if successful_groups != len(self.INVESTORS):
                raise KrxDataError(
                    f"KRX investor flows are incomplete for the {days}-trading-day window"
                )
            all_flows[days] = period
        return all_flows, names

    @staticmethod
    def _stock_module() -> Any:
        try:
            from pykrx import stock  # type: ignore[import-untyped]
        except ImportError as exc:
            raise KrxDataError("pykrx is not installed") from exc
        return stock

    @staticmethod
    def _decimal(value: object) -> Decimal:
        try:
            return Decimal(str(value))
        except Exception as exc:
            raise KrxDataError(f"KRX returned a non-numeric value: {value!r}") from exc
