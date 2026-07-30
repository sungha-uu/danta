from __future__ import annotations

import asyncio
import json
import subprocess
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from danta.adapters.kis.client import KisApiError, KisClient
from danta.domain.market_wide import (
    DailyMarketFlow,
    FlowQualityFeatures,
    InvestorNetFlow,
    MarketWideRiskLevel,
    MarketWideSnapshot,
    ProgramNetFlow,
)
from danta.services.market_guard import (
    MarketGuardDecision,
    MarketGuardObservation,
    MarketRegimeGuard,
)
from danta.services.market_wide_repository import (
    MarketWideRepository,
    market_status_payload,
)

TransitionCallback = Callable[
    [MarketWideSnapshot, MarketGuardDecision, MarketWideRiskLevel | None],
    Awaitable[None],
]

_RISK_LEVEL_ORDER = {
    MarketWideRiskLevel.NORMAL: 0,
    MarketWideRiskLevel.CAUTION: 1,
    MarketWideRiskLevel.RISK_OFF: 2,
    MarketWideRiskLevel.PANIC: 3,
}


def is_market_risk_escalation(
    previous: MarketWideRiskLevel | None,
    current: MarketWideRiskLevel,
) -> bool:
    """Return true only for a first warning or a worsening market state."""
    if current is MarketWideRiskLevel.NORMAL:
        return False
    if previous is None:
        return True
    return _RISK_LEVEL_ORDER[current] > _RISK_LEVEL_ORDER[previous]


class MarketWideCollector:
    """Collect KOSPI index, investor flow and program flow from KIS REST."""

    def __init__(self, client: KisClient) -> None:
        self._client = client
        self._daily_flows: list[DailyMarketFlow] = []
        self._daily_refresh_pending = False
        self._daily_anchor = ""

    async def collect(self) -> MarketWideSnapshot:
        observed_at = datetime.now(UTC)
        index = await self._client.kospi_index_price()
        complete = True
        try:
            investor = await self._client.kospi_investor_flows()
        except KisApiError:
            investor = InvestorNetFlow(*(0 for _ in range(11)))
            complete = False
        try:
            program = await self._client.kospi_program_flows()
        except KisApiError:
            program = ProgramNetFlow(0, 0, 0)
            complete = False
        anchor = observed_at.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        if anchor != self._daily_anchor:
            try:
                self._daily_flows = await self._client.kospi_daily_investor_flows(
                    anchor_date=anchor,
                    limit=10,
                )
                self._daily_anchor = anchor
                self._daily_refresh_pending = True
            except KisApiError:
                # Historical continuity is informative but must not disable
                # the real-time protective path.
                pass
        return MarketWideSnapshot(
            observed_at=observed_at,
            kospi_index=index.index,
            kospi_return_pct=index.return_pct,
            kospi_open=index.open,
            kospi_high=index.high,
            kospi_low=index.low,
            accumulated_trading_value_million=(
                index.accumulated_trading_value_million
            ),
            rising_issues=index.rising_issues,
            flat_issues=index.flat_issues,
            declining_issues=index.declining_issues,
            upper_limit_issues=index.upper_limit_issues,
            lower_limit_issues=index.lower_limit_issues,
            investor=investor,
            program=program,
            flow_quality=build_flow_quality(self._daily_flows),
            provider_complete=complete,
        )

    def consume_daily_refresh(self) -> list[DailyMarketFlow]:
        if not self._daily_refresh_pending:
            return []
        self._daily_refresh_pending = False
        return list(self._daily_flows)


class MarketWideMonitor:
    """Local deterministic monitor. It never depends on Pages or an LLM."""

    def __init__(
        self,
        *,
        collector: MarketWideCollector,
        repository: MarketWideRepository,
        guard: MarketRegimeGuard | None = None,
        on_transition: TransitionCallback | None = None,
    ) -> None:
        self._collector = collector
        self._repository = repository
        self._guard = guard or MarketRegimeGuard()
        self._on_transition = on_transition
        self._history: deque[MarketWideSnapshot] = deque(maxlen=240)
        self._last_level: MarketWideRiskLevel | None = None

    async def poll_once(self) -> tuple[MarketWideSnapshot, MarketGuardDecision]:
        snapshot = self._with_deltas(await self._collector.collect())
        daily_flows = self._collector.consume_daily_refresh()
        if daily_flows:
            await self._repository.save_daily_flows(daily_flows)
        decision = self._guard.observe(
            MarketGuardObservation(
                kospi_return_pct=snapshot.kospi_return_pct,
                declining_issue_ratio=snapshot.declining_issue_ratio,
                foreign_net_ratio=snapshot.foreign_net_ratio,
                foreign_delta_5m=snapshot.foreign_delta_5m,
                pension_net_million=snapshot.investor.pension_fund_etc,
                program_net_million=snapshot.program.total,
                program_delta_5m=snapshot.program_delta_5m,
                provider_complete=snapshot.provider_complete,
                market_emergency=(
                    snapshot.lower_limit_issues >= 10
                    or (
                        snapshot.kospi_return_pct <= -8
                        and snapshot.declining_issue_ratio >= 0.9
                    )
                ),
            )
        )
        await self._repository.save(snapshot, decision)
        previous = self._last_level
        self._last_level = decision.level
        self._history.append(snapshot)
        if (
            self._on_transition is not None
            and (
                (previous is None and decision.level is not MarketWideRiskLevel.NORMAL)
                or (previous is not None and previous is not decision.level)
            )
        ):
            await self._on_transition(snapshot, decision, previous)
        return snapshot, decision

    async def run(
        self,
        *,
        interval_seconds: float,
        on_snapshot: Callable[
            [MarketWideSnapshot, MarketGuardDecision], Awaitable[None]
        ]
        | None = None,
    ) -> None:
        if interval_seconds < 10:
            raise ValueError("market-wide REST poll interval must be at least 10 seconds")
        while True:
            started = asyncio.get_running_loop().time()
            snapshot, decision = await self.poll_once()
            if on_snapshot is not None:
                await on_snapshot(snapshot, decision)
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(1, interval_seconds - elapsed))

    def _with_deltas(self, current: MarketWideSnapshot) -> MarketWideSnapshot:
        five = _reference(self._history, current.observed_at - timedelta(minutes=5))
        fifteen = _reference(
            self._history, current.observed_at - timedelta(minutes=15)
        )
        return replace(
            current,
            foreign_delta_5m=_flow_delta(
                current.investor.foreign, five, "foreign"
            ),
            foreign_delta_15m=_flow_delta(
                current.investor.foreign, fifteen, "foreign"
            ),
            institution_delta_5m=_flow_delta(
                current.investor.institution, five, "institution"
            ),
            institution_delta_15m=_flow_delta(
                current.investor.institution, fifteen, "institution"
            ),
            pension_delta_5m=_flow_delta(
                current.investor.pension_fund_etc, five, "pension"
            ),
            pension_delta_15m=_flow_delta(
                current.investor.pension_fund_etc, fifteen, "pension"
            ),
            program_delta_5m=_flow_delta(
                current.program.total, five, "program"
            ),
            program_delta_15m=_flow_delta(
                current.program.total, fifteen, "program"
            ),
        )


class MarketStatusPublisher:
    """Write a sanitized Pages data file and optionally commit/push only that file."""

    def __init__(
        self,
        *,
        repository_path: Path,
        relative_path: Path = Path("data/market-status.json"),
        git_push_enabled: bool = True,
    ) -> None:
        self.repository_path = repository_path.resolve()
        self.relative_path = relative_path
        self.git_push_enabled = git_push_enabled
        self._lock = asyncio.Lock()

    async def publish(
        self,
        snapshot: MarketWideSnapshot,
        decision: MarketGuardDecision,
    ) -> Path:
        async with self._lock:
            return await asyncio.to_thread(
                self._publish_sync,
                market_status_payload(snapshot, decision),
            )

    def _publish_sync(self, payload: dict[str, object]) -> Path:
        target = (self.repository_path / self.relative_path).resolve()
        if self.repository_path not in target.parents:
            raise ValueError("market status path must stay inside publish repository")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        if self.git_push_enabled:
            self._commit_and_push(target)
        return target

    def _commit_and_push(self, target: Path) -> None:
        if not (self.repository_path / ".git").exists():
            raise RuntimeError("dashboard publish repository is not a Git repository")
        relative = target.relative_to(self.repository_path).as_posix()
        staged_before = _git(
            self.repository_path, "diff", "--cached", "--name-only"
        ).splitlines()
        if staged_before and staged_before != [relative]:
            raise RuntimeError("publish repository has unrelated staged changes")
        _git(self.repository_path, "add", "--", relative)
        staged = _git(self.repository_path, "diff", "--cached", "--name-only")
        if relative not in staged.splitlines():
            return
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        _git(
            self.repository_path,
            "commit",
            "-m",
            f"[market] update risk snapshot {timestamp}",
            "--",
            relative,
        )
        _git(self.repository_path, "push")


def _reference(
    history: deque[MarketWideSnapshot],
    target: datetime,
) -> MarketWideSnapshot | None:
    eligible = [item for item in history if item.observed_at <= target]
    return eligible[-1] if eligible else None


def _flow_delta(
    current: int,
    reference: MarketWideSnapshot | None,
    field: str,
) -> int:
    if reference is None:
        return 0
    values = {
        "foreign": reference.investor.foreign,
        "institution": reference.investor.institution,
        "pension": reference.investor.pension_fund_etc,
        "program": reference.program.total,
    }
    return current - values[field]


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"Git market-status publish failed: {detail}")
    return completed.stdout.strip()


def build_flow_quality(flows: list[DailyMarketFlow]) -> FlowQualityFeatures | None:
    if not flows:
        return None
    ordered = sorted(flows, key=lambda item: item.trading_date, reverse=True)
    latest = ordered[0]
    combined = latest.foreign + latest.pension_fund_etc + latest.investment_trust
    if combined > 0 and latest.kospi_return_pct >= 0:
        regime = "PRICE_UP_WITH_CORE_BUYING"
    elif combined > 0 and latest.kospi_return_pct < 0:
        regime = "CORE_BUYING_ABSORBING_DECLINE"
    elif combined < 0 and latest.kospi_return_pct >= 0:
        regime = "PRICE_UP_WITH_CORE_SELLING"
    else:
        regime = "PRICE_DOWN_WITH_CORE_SELLING"
    return FlowQualityFeatures(
        as_of_date=latest.trading_date,
        foreign_positive_streak=_positive_streak(
            [item.foreign for item in ordered]
        ),
        pension_positive_streak=_positive_streak(
            [item.pension_fund_etc for item in ordered]
        ),
        investment_trust_positive_streak=_positive_streak(
            [item.investment_trust for item in ordered]
        ),
        joint_positive_days_5=sum(
            item.foreign > 0
            and item.pension_fund_etc > 0
            and item.investment_trust > 0
            for item in ordered[:5]
        ),
        joint_positive_days_10=sum(
            item.foreign > 0
            and item.pension_fund_etc > 0
            and item.investment_trust > 0
            for item in ordered[:10]
        ),
        financial_investment_only_warning=(
            latest.institution > 0
            and latest.financial_investment > 0
            and latest.pension_fund_etc <= 0
            and latest.investment_trust <= 0
        ),
        latest_price_flow_regime=regime,
    )


def _positive_streak(values: list[int]) -> int:
    streak = 0
    for value in values:
        if value <= 0:
            break
        streak += 1
    return streak
