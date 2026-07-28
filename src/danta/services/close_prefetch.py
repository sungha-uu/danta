from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from danta.adapters.kis.client import KisClient
from danta.adapters.krx.client import PykrxMarketDataClient
from danta.config import (
    AppSettings,
    load_kis_credentials,
    load_krx_environment,
)
from danta.services.daily_operations import _exclusive_lock
from danta.services.intraday_report import (
    MinuteBarStore,
    backfill_minute_bars,
    market_cap_top_universe,
)

KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class ClosePrefetchResult:
    status: Literal["COMPLETED", "ALREADY_COMPLETED", "NON_TRADING_DAY"]
    trade_date: str
    completed_at: str
    symbol_count: int


async def run_close_prefetch(
    settings: AppSettings,
    *,
    now: datetime | None = None,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ClosePrefetchResult:
    current = (now or datetime.now(KST)).astimezone(KST)
    trade_date = current.date().isoformat()
    emit = progress if progress is not None else lambda _message: None
    if current.weekday() >= 5 and not force:
        return ClosePrefetchResult(
            status="NON_TRADING_DAY",
            trade_date=trade_date,
            completed_at=current.isoformat(),
            symbol_count=0,
        )
    settings.daily_run_root.mkdir(parents=True, exist_ok=True)
    marker = settings.daily_run_root / f"{trade_date}-prefetch.json"
    if marker.exists() and not force:
        return ClosePrefetchResult(
            status="ALREADY_COMPLETED",
            trade_date=trade_date,
            completed_at=current.isoformat(),
            symbol_count=0,
        )
    with _exclusive_lock(settings.daily_run_root / "close-prefetch.lock"):
        load_krx_environment(settings)
        dataset = PykrxMarketDataClient().collect(required_days=21)
        if dataset.trading_dates[-1] != current.date() and not force:
            return ClosePrefetchResult(
                status="NON_TRADING_DAY",
                trade_date=trade_date,
                completed_at=datetime.now(KST).isoformat(),
                symbol_count=0,
            )
        universe = market_cap_top_universe(dataset, limit=200)
        credentials = load_kis_credentials(settings)
        async with KisClient(
            credentials,
            token_cache_path=Path("data/kis-token-cache.json"),
        ) as client:
            await backfill_minute_bars(
                client,
                MinuteBarStore(Path("data/intraday/1m")),
                universe,
                [current.date()],
                window_days=7,
                progress=emit,
            )
        result = ClosePrefetchResult(
            status="COMPLETED",
            trade_date=trade_date,
            completed_at=datetime.now(KST).isoformat(),
            symbol_count=len(universe),
        )
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(marker)
        return result
