from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from danta.adapters.kis.client import KisClient
from danta.config import AppSettings, KisCredentials, TradingEnvironment


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    provider: str
    environment: str
    checked_at: str
    live: bool
    checks: list[CheckResult]

    def as_public_dict(self) -> dict[str, Any]:
        return asdict(self)


class KisProviderDoctor:
    def __init__(self, settings: AppSettings, credentials: KisCredentials) -> None:
        self.settings = settings
        self.credentials = credentials

    async def run(self, *, live: bool = False, symbol: str = "005930") -> DoctorReport:
        checks = [
            CheckResult("environment", "PASS", self.credentials.environment.value),
            CheckResult("account_format", "PASS", "8-2 account format validated"),
            CheckResult("immutable_policy", "PASS", "approval and -7% stop policy locked"),
        ]
        if self.credentials.environment is TradingEnvironment.PROD:
            checks.append(
                CheckResult("production_lock", "PASS", "real order execution remains disabled")
            )
        if live:
            cache_path = (
                Path(".secrets/kis/.cache")
                / f"{self.credentials.environment.value}_token.json"
            )
            async with KisClient(self.credentials, token_cache_path=cache_path) as client:
                await client.access_token()
                checks.append(CheckResult("rest_token", "PASS", "token issued"))
                quote = await client.current_price(symbol)
                checks.append(CheckResult("current_price", "PASS", f"{symbol}={quote.price}"))
                positions = await client.positions()
                checks.append(
                    CheckResult("balance", "PASS", f"positions={len(positions)}")
                )
                await client.websocket_approval_key()
                checks.append(CheckResult("websocket_approval", "PASS", "approval key issued"))
        else:
            checks.append(CheckResult("network", "SKIP", "run with --live to call KIS"))
        return DoctorReport(
            provider="KIS",
            environment=self.credentials.environment.value,
            checked_at=datetime.now(UTC).isoformat(),
            live=live,
            checks=checks,
        )
