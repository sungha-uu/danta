from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from danta.adapters.kis.client import KisApiError, KisClient
from danta.config import AppSettings, KisCredentials, TradingEnvironment
from danta.dashboard.builder import load_dashboard_report
from danta.dashboard.models import CandidateView
from danta.domain.mandate import EntryMandate, EntrySelection
from danta.domain.market import MarketRisk
from danta.domain.trading_session import OrchestratorState
from danta.services.command_store import FileCommandStore
from danta.services.market_session import TradingSessionPhase, trading_session_phase
from danta.services.notifier import NotificationError, SmtpNotifier
from danta.services.runtime_repository import SqlRuntimeRepository
from danta.services.trading_runtime import TradingRuntimeCore

AUTONOMOUS_GRADES = frozenset({"STRONG_RECOMMEND", "RECOMMEND"})
AUTONOMOUS_SELECTION_VERSION = "paper-autonomous-rank-first-v2"
KST = ZoneInfo("Asia/Seoul")


class PaperAutonomousCampaignAuthorization(BaseModel):
    schema_version: Literal[1] = 1
    authority: Literal["PAPER_AUTONOMOUS_CAMPAIGN"]
    environment: Literal["paper"]
    campaign_id: str = Field(pattern=r"^paper-auto-[a-z0-9-]{8,64}$")
    approved_at: datetime
    expires_at: datetime
    enabled: bool = True
    max_concurrent_positions: int = Field(default=3, ge=1, le=3)
    max_daily_batches: int = Field(default=1, ge=1, le=3)
    max_capital_pct: Decimal = Field(default=Decimal("100.0"), gt=0, le=100)
    approved_grades: tuple[Literal["STRONG_RECOMMEND", "RECOMMEND"], ...] = (
        "STRONG_RECOMMEND",
        "RECOMMEND",
    )

    @model_validator(mode="after")
    def validate_window(self) -> PaperAutonomousCampaignAuthorization:
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("campaign timestamps must be timezone-aware")
        duration = self.expires_at.astimezone(UTC) - self.approved_at.astimezone(UTC)
        if duration <= timedelta(0) or duration > timedelta(days=90):
            raise ValueError("campaign duration must be positive and at most 90 days")
        if len(set(self.approved_grades)) != len(self.approved_grades):
            raise ValueError("approved_grades must be unique")
        return self

    def permits_new_entries(self, now: datetime) -> bool:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        normalized = now.astimezone(UTC)
        return self.enabled and self.approved_at.astimezone(
            UTC
        ) <= normalized < self.expires_at.astimezone(UTC)


class PaperAutonomousCampaignState(BaseModel):
    schema_version: Literal[1] = 1
    campaign_id: str
    submitted_reports: list[str] = Field(default_factory=list, max_length=100)
    daily_batches: dict[str, int] = Field(default_factory=dict)
    updated_at: datetime


def create_campaign_authorization(
    *,
    now: datetime,
    days: int = 90,
) -> PaperAutonomousCampaignAuthorization:
    if days < 1 or days > 90:
        raise ValueError("days must be between 1 and 90")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return PaperAutonomousCampaignAuthorization(
        authority="PAPER_AUTONOMOUS_CAMPAIGN",
        environment="paper",
        campaign_id=f"paper-auto-{uuid4().hex[:16]}",
        approved_at=now,
        expires_at=now + timedelta(days=days),
    )


def write_campaign_authorization(
    authorization: PaperAutonomousCampaignAuthorization,
    path: Path,
) -> None:
    _atomic_json(path, authorization.model_dump(mode="json"))


def load_campaign_authorization(
    settings: AppSettings,
    credentials: KisCredentials,
) -> PaperAutonomousCampaignAuthorization | None:
    path = settings.paper_autonomous_campaign_path
    if not path.exists():
        return None
    # Fail closed even if a production config points at a paper authorization.
    if (
        settings.environment is not TradingEnvironment.PAPER
        or credentials.environment is not TradingEnvironment.PAPER
        or settings.real_order_execution_enabled
    ):
        raise PermissionError("paper autonomous campaign is forbidden outside paper")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return PaperAutonomousCampaignAuthorization.model_validate(payload)


class PaperAutonomousCampaignController:
    """Generate one durable internal mandate per reviewed report.

    The controller never calls an order endpoint itself. It submits through the
    same command store, orchestrator, order manager and reconciliation path as a
    manual mandate, so real KIS paper API behavior is exercised without creating
    a second execution implementation.
    """

    def __init__(
        self,
        *,
        settings: AppSettings,
        credentials: KisCredentials,
        command_store: FileCommandStore,
        core: TradingRuntimeCore,
        repository: SqlRuntimeRepository,
        notifier: SmtpNotifier | None = None,
        broker: KisClient | None = None,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.command_store = command_store
        self.core = core
        self.repository = repository
        self.notifier = notifier
        self.broker = broker
        self._last_block_reason: str | None = None
        self.state_path = settings.paper_autonomous_campaign_path.with_name(
            "paper_autonomous_campaign_state.json"
        )

    async def run(self) -> None:
        interval = self.settings.paper_autonomous_poll_interval_seconds
        while True:
            try:
                await self.tick(datetime.now(UTC))
            except Exception as exc:
                await self.repository.audit(
                    "PAPER_AUTONOMOUS_CAMPAIGN_ERROR",
                    correlation_id="paper-autonomous-campaign",
                    payload={
                        "error": type(exc).__name__,
                        "detail": str(exc)[:240],
                    },
                )
            await asyncio.sleep(interval)

    async def tick(self, now: datetime) -> EntryMandate | None:
        authorization = load_campaign_authorization(self.settings, self.credentials)
        if authorization is None:
            return None
        reason = self._blocking_reason(authorization, now)
        if reason is not None:
            if reason != self._last_block_reason:
                await self.repository.audit(
                    "PAPER_AUTONOMOUS_ENTRY_BLOCKED",
                    correlation_id=authorization.campaign_id,
                    payload={"reason": reason},
                )
                self._last_block_reason = reason
            return None
        self._last_block_reason = None

        report = load_dashboard_report(self.settings.paper_autonomous_report_path)
        if not report.model_id.startswith("agent-"):
            await self._blocked(authorization, "REPORT_NOT_AGENT_REVIEWED")
            return None
        report_key = report.data_as_of.isoformat()
        expected_date = _latest_expected_report_date(now)
        if report.data_as_of.astimezone(KST).date() < expected_date:
            await self._blocked(authorization, "REPORT_STALE")
            return None
        if self.broker is None:
            await self._blocked(authorization, "LIVE_QUOTE_PROVIDER_UNAVAILABLE")
            return None

        state = self._load_state(authorization)
        trading_day = now.astimezone(KST).date().isoformat()
        if report_key in state.submitted_reports:
            return None
        if state.daily_batches.get(trading_day, 0) >= authorization.max_daily_batches:
            await self._blocked(authorization, "DAILY_BATCH_LIMIT")
            return None

        all_candidates = [*report.candidates, *report.extended_watchlist]
        eligible = [
            candidate
            for candidate in all_candidates
            if candidate.context_status == "READY"
            and candidate.windows["14"].structure_status == "READY"
            and candidate.windows["14"].ai_grade in authorization.approved_grades
            and candidate.windows["14"].rank is not None
            and candidate.windows["14"].rank <= 50
        ]
        live_candidates: list[tuple[int, int, Decimal, CandidateView, int]] = []
        overlay_rows: list[dict[str, object]] = []
        for candidate in eligible:
            metrics = candidate.windows["14"]
            if metrics.box_low is None or metrics.box_high is None or metrics.rank is None:
                continue
            try:
                quote = await self.broker.current_price(candidate.code)
            except KisApiError as exc:
                await self.repository.audit(
                    "PAPER_AUTONOMOUS_LIVE_QUOTE_ERROR",
                    correlation_id=authorization.campaign_id,
                    payload={
                        "symbol": candidate.code,
                        "error": type(exc).__name__,
                    },
                )
                continue
            position_pct = (
                (Decimal(quote.price) - metrics.box_low)
                / (metrics.box_high - metrics.box_low)
                * Decimal("100")
            )
            grade_priority = 0 if metrics.ai_grade == "STRONG_RECOMMEND" else 1
            live_candidates.append(
                (
                    metrics.rank,
                    grade_priority,
                    position_pct,
                    candidate,
                    quote.price,
                )
            )
            overlay_rows.append(
                {
                    "symbol": candidate.code,
                    "name": candidate.name,
                    "base_price": str(candidate.current_price),
                    "live_price": quote.price,
                    "live_position_pct": str(position_pct.quantize(Decimal("0.01"))),
                    "ai_grade": metrics.ai_grade,
                    "rank_14d": metrics.rank,
                }
            )
        # AI approval remains mandatory, but repeated-rise rank must decide which
        # approved opportunities receive scarce autonomous slots. Otherwise a
        # low-position rank-40 candidate can displace a rank-2 candidate.
        live_candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        selected = live_candidates[: authorization.max_concurrent_positions]
        if not selected:
            await self._blocked(authorization, "NO_APPROVED_CANDIDATES")
            return None
        _atomic_json(
            self.settings.paper_autonomous_campaign_path.with_name(
                "paper_autonomous_live_overlay.json"
            ),
            {
                "schema_version": 1,
                "observed_at": now.isoformat(),
                "report_data_as_of": report_key,
                "rows": overlay_rows,
            },
        )

        count = len(selected)
        allocation = (authorization.max_capital_pct / Decimal(count)).quantize(Decimal("0.1"))
        allocations = [allocation] * count
        # Keep totals exact while preserving one decimal place.
        allocations[-1] += authorization.max_capital_pct - sum(allocations, Decimal("0"))
        selections: list[EntrySelection] = []
        for live_item, allocation_pct in zip(selected, allocations, strict=True):
            candidate = live_item[3]
            live_price = live_item[4]
            metrics = candidate.windows["14"]
            if metrics.box_low is None or metrics.box_high is None or metrics.rank is None:
                continue
            selections.append(
                EntrySelection(
                    rank=metrics.rank,
                    symbol=candidate.code,
                    name=candidate.name,
                    entry_target_price_krw=live_price,
                    entry_price_source="PAPER_AUTONOMOUS_REPORT_PRICE",
                    allocation_pct=allocation_pct,
                    ai_grade=str(metrics.ai_grade),
                    box_low=metrics.box_low,
                    box_high=metrics.box_high,
                )
            )
        if not selections:
            await self._blocked(authorization, "NO_VALID_SELECTIONS")
            return None
        total = sum((item.allocation_pct for item in selections), Decimal("0"))
        mandate = EntryMandate(
            report_data_as_of=report.data_as_of,
            window_days=14,
            authority="ENTRY_APPROVAL",
            execution_mode="USE_LOCKED_ACTIVE_MODE",
            capital_scope="KIS_ORDERABLE_CASH",
            allocation_policy="USER_DEFINED_ORDERABLE_CASH_PERCENT",
            total_allocation_pct=total,
            unallocated_cash_pct=Decimal("100.0") - total,
            selected_symbol_count=len(selections),
            entry_trigger="LAST_PRICE_LTE_TARGET",
            validity_policy="UNTIL_FILLED_OR_USER_CANCELLED",
            partial_fill_policy=("PROTECT_FILLED_CANCEL_REMAINDER_ON_SAFETY_DETERIORATION"),
            duplicate_guard="INTERNAL_ON_INGEST",
            hard_stop_pct=Decimal("-7.0"),
            profit_policy="ACTIVE_VERSIONED_LOCAL_ENGINE",
            selections=selections,
            request=f"PAPER_AUTONOMOUS_CAMPAIGN:{authorization.campaign_id}",
        )
        self.command_store.submit(mandate)
        state.submitted_reports.append(report_key)
        state.daily_batches[trading_day] = state.daily_batches.get(trading_day, 0) + 1
        state.updated_at = now
        _atomic_json(self.state_path, state.model_dump(mode="json"))
        await self.repository.audit(
            "PAPER_AUTONOMOUS_MANDATE_SUBMITTED",
            correlation_id=authorization.campaign_id,
            payload={
                "command_id": mandate.command_id,
                "selection_version": AUTONOMOUS_SELECTION_VERSION,
                "report_data_as_of": report_key,
                "symbols": [item.symbol for item in selections],
            },
        )
        selected_positions = {
            candidate.code: position_pct
            for _, _, position_pct, candidate, _ in selected
        }
        await self._notify_selections(
            authorization,
            selections,
            selected_positions,
        )
        await self._notify_prices(authorization, selections)
        return mandate

    def _blocking_reason(
        self,
        authorization: PaperAutonomousCampaignAuthorization,
        now: datetime,
    ) -> str | None:
        if not authorization.permits_new_entries(now):
            return "CAMPAIGN_DISABLED_OR_EXPIRED"
        if self.settings.paper_autonomous_kill_switch_path.exists():
            return "KILL_SWITCH_ACTIVE"
        if trading_session_phase(now) is not TradingSessionPhase.KRX_REGULAR:
            return "OUTSIDE_REGULAR_SESSION"
        if self.command_store.load_active() is not None:
            return "ACTIVE_MANUAL_OR_AUTONOMOUS_COMMAND"
        if self.core.positions or self.core.submitted:
            return "ACCOUNT_NOT_FLAT"
        if self.core.orchestrator.state is not OrchestratorState.RUNNING:
            return "ORCHESTRATOR_NOT_READY"
        if self.core.market_risk is not MarketRisk.NORMAL:
            return "MARKET_RISK_NOT_NORMAL"
        return None

    async def _blocked(
        self,
        authorization: PaperAutonomousCampaignAuthorization,
        reason: str,
    ) -> None:
        await self.repository.audit(
            "PAPER_AUTONOMOUS_ENTRY_BLOCKED",
            correlation_id=authorization.campaign_id,
            payload={"reason": reason},
        )
        return None

    async def _notify_prices(
        self,
        authorization: PaperAutonomousCampaignAuthorization,
        selections: list[EntrySelection],
    ) -> None:
        if self.notifier is None:
            await self.repository.audit(
                "PAPER_AUTONOMOUS_PRICE_EMAIL_SKIPPED",
                correlation_id=authorization.campaign_id,
                payload={"reason": "SMTP_DISABLED_OR_UNAVAILABLE"},
            )
            return
        prices = [(selection.name, selection.entry_target_price_krw) for selection in selections]
        retry_delays = (2.0, 10.0)
        for attempt in range(3):
            try:
                receipt = await asyncio.to_thread(
                    self.notifier.send_entry_prices_determined,
                    prices,
                )
                await self.repository.audit(
                    "PAPER_AUTONOMOUS_PRICE_EMAIL_SENT",
                    correlation_id=authorization.campaign_id,
                    payload={
                        "symbols": [selection.symbol for selection in selections],
                        "recipient_count": receipt.recipient_count,
                    },
                )
                return
            except (NotificationError, OSError) as exc:
                await self.repository.audit(
                    "PAPER_AUTONOMOUS_PRICE_EMAIL_ERROR",
                    correlation_id=authorization.campaign_id,
                    payload={
                        "attempt": attempt + 1,
                        "error": type(exc).__name__,
                    },
                )
                if attempt < len(retry_delays):
                    await asyncio.sleep(retry_delays[attempt])

    async def _notify_selections(
        self,
        authorization: PaperAutonomousCampaignAuthorization,
        selections: list[EntrySelection],
        selected_positions: dict[str, Decimal],
    ) -> None:
        if self.notifier is None:
            await self.repository.audit(
                "PAPER_AUTONOMOUS_SELECTION_EMAIL_SKIPPED",
                correlation_id=authorization.campaign_id,
                payload={"reason": "SMTP_DISABLED_OR_UNAVAILABLE"},
            )
            return
        rows = [
            (
                selection.name,
                selection.ai_grade,
                selection.entry_target_price_krw,
                selected_positions[selection.symbol],
            )
            for selection in selections
        ]
        retry_delays = (2.0, 10.0)
        for attempt in range(3):
            try:
                receipt = await asyncio.to_thread(
                    self.notifier.send_autonomous_selection_completed,
                    rows,
                )
                await self.repository.audit(
                    "PAPER_AUTONOMOUS_SELECTION_EMAIL_SENT",
                    correlation_id=authorization.campaign_id,
                    payload={
                        "symbols": [selection.symbol for selection in selections],
                        "recipient_count": receipt.recipient_count,
                    },
                )
                return
            except (NotificationError, OSError) as exc:
                await self.repository.audit(
                    "PAPER_AUTONOMOUS_SELECTION_EMAIL_ERROR",
                    correlation_id=authorization.campaign_id,
                    payload={
                        "attempt": attempt + 1,
                        "error": type(exc).__name__,
                    },
                )
                if attempt < len(retry_delays):
                    await asyncio.sleep(retry_delays[attempt])

    def _load_state(
        self,
        authorization: PaperAutonomousCampaignAuthorization,
    ) -> PaperAutonomousCampaignState:
        if not self.state_path.exists():
            return PaperAutonomousCampaignState(
                campaign_id=authorization.campaign_id,
                updated_at=datetime.now(UTC),
            )
        state = PaperAutonomousCampaignState.model_validate_json(
            self.state_path.read_text(encoding="utf-8")
        )
        if state.campaign_id != authorization.campaign_id:
            return PaperAutonomousCampaignState(
                campaign_id=authorization.campaign_id,
                updated_at=datetime.now(UTC),
            )
        return state


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _latest_expected_report_date(now: datetime) -> date:
    local_date = now.astimezone(KST).date()
    candidate = local_date - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate
