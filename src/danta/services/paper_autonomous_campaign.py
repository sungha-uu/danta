from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, TypedDict
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from danta.adapters.kis.client import KisClient
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

AUTONOMOUS_SELECTION_VERSION = "quant-discount-live-flow-v5"
KST = ZoneInfo("Asia/Seoul")


def _autonomous_opportunity_score(
    *,
    live_rank: int,
    discount_from_high_pct: Decimal,
    flow_strength_pct: Decimal,
) -> Decimal:
    """Score live entry opportunities without making positive flow mandatory."""
    bounded_discount = min(max(discount_from_high_pct, Decimal("0")), Decimal("50"))
    bounded_rank = min(max(live_rank, 1), 50)
    bounded_flow = min(max(flow_strength_pct, Decimal("-3")), Decimal("3"))
    discount_score = bounded_discount / Decimal("50") * Decimal("40")
    rank_score = Decimal(50 - bounded_rank) / Decimal("49") * Decimal("40")
    flow_score = (bounded_flow + Decimal("3")) / Decimal("6") * Decimal("20")
    return (discount_score + rank_score + flow_score).quantize(Decimal("0.0001"))


class IntradayOverlayRow(TypedDict):
    symbol: str
    live_rank_14d: int
    live_price: int
    live_position_pct: Decimal
    discount_from_window_high_pct: Decimal
    intraday_flow_status: str
    intraday_combined_net_qty: int
    intraday_flow_strength_pct: Decimal


class IntradayOverlayPayload(TypedDict):
    revision: str
    rows: list[IntradayOverlayRow]


@dataclass(frozen=True, slots=True)
class _LiveCandidate:
    live_rank: int
    position_pct: Decimal
    candidate: CandidateView
    live_price: int
    discount_from_high_pct: Decimal
    flow_strength_pct: Decimal
    opportunity_score: Decimal


class AutonomousCampaignAuthorization(BaseModel):
    schema_version: Literal[1] = 1
    authority: Literal["AUTONOMOUS_TRADING_CAMPAIGN"]
    environment: Literal["paper", "prod"]
    campaign_id: str = Field(pattern=r"^(paper|live)-auto-[a-z0-9-]{8,64}$")
    approved_at: datetime
    expires_at: datetime
    enabled: bool = True
    max_concurrent_positions: int = Field(default=3, ge=1, le=3)
    max_daily_batches: int = Field(default=3, ge=1, le=3)
    max_capital_pct: Decimal = Field(default=Decimal("100.0"), gt=0, le=100)

    @model_validator(mode="after")
    def validate_window(self) -> AutonomousCampaignAuthorization:
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("campaign timestamps must be timezone-aware")
        duration = self.expires_at.astimezone(UTC) - self.approved_at.astimezone(UTC)
        if duration <= timedelta(0) or duration > timedelta(days=90):
            raise ValueError("campaign duration must be positive and at most 90 days")
        return self

    def permits_new_entries(self, now: datetime) -> bool:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        normalized = now.astimezone(UTC)
        return self.enabled and self.approved_at.astimezone(
            UTC
        ) <= normalized < self.expires_at.astimezone(UTC)


class AutonomousCampaignState(BaseModel):
    schema_version: Literal[1] = 1
    campaign_id: str
    submitted_reports: list[str] = Field(default_factory=list, max_length=100)
    daily_batches: dict[str, int] = Field(default_factory=dict)
    notification_keys: list[str] = Field(default_factory=list, max_length=100)
    updated_at: datetime


class AutonomousCandidatePreference(BaseModel):
    """One-day user preference that changes candidate ordering, not safety gates."""

    schema_version: Literal[1] = 1
    authority: Literal["USER_CANDIDATE_PREFERENCE"] = "USER_CANDIDATE_PREFERENCE"
    trading_date: date
    symbols: tuple[str, ...] = Field(min_length=1, max_length=3)
    selection_policy: Literal["PRIORITIZE_ELIGIBLE", "REQUIRE_INCLUDE"] = (
        "PRIORITIZE_ELIGIBLE"
    )
    created_at: datetime

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("preference symbols must be unique")
        if any(len(symbol) != 6 or not symbol.isdigit() for symbol in values):
            raise ValueError("preference symbols must be six digits")
        return values

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("preference created_at must be timezone-aware")
        return value


def candidate_preference_path(settings: AppSettings) -> Path:
    return settings.autonomous_campaign_path.with_name(
        "autonomous_candidate_preference.json"
    )


def write_candidate_preference(
    preference: AutonomousCandidatePreference,
    path: Path,
) -> None:
    _atomic_json(path, preference.model_dump(mode="json"))


def load_candidate_preference(
    settings: AppSettings,
    now: datetime,
) -> AutonomousCandidatePreference | None:
    preference = read_candidate_preference(settings)
    if preference is None:
        return None
    if preference.trading_date != now.astimezone(KST).date():
        return None
    return preference


def read_candidate_preference(
    settings: AppSettings,
) -> AutonomousCandidatePreference | None:
    path = candidate_preference_path(settings)
    if not path.exists():
        return None
    preference = AutonomousCandidatePreference.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    return preference


def create_campaign_authorization(
    *,
    now: datetime,
    days: int = 90,
    environment: TradingEnvironment = TradingEnvironment.PAPER,
) -> AutonomousCampaignAuthorization:
    if days < 1 or days > 90:
        raise ValueError("days must be between 1 and 90")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    prefix = "live" if environment is TradingEnvironment.PROD else "paper"
    return AutonomousCampaignAuthorization(
        authority="AUTONOMOUS_TRADING_CAMPAIGN",
        environment=environment.value,
        campaign_id=f"{prefix}-auto-{uuid4().hex[:16]}",
        approved_at=now,
        expires_at=now + timedelta(days=days),
    )


def write_campaign_authorization(
    authorization: AutonomousCampaignAuthorization,
    path: Path,
) -> None:
    _atomic_json(path, authorization.model_dump(mode="json"))


def load_campaign_authorization(
    settings: AppSettings,
    credentials: KisCredentials,
) -> AutonomousCampaignAuthorization | None:
    path = settings.autonomous_campaign_path
    if not path.exists():
        return None
    if settings.environment is not credentials.environment:
        raise PermissionError("autonomous campaign environment mismatch")
    payload = json.loads(path.read_text(encoding="utf-8"))
    authorization = AutonomousCampaignAuthorization.model_validate(payload)
    if authorization.environment != settings.environment.value:
        if authorization.environment == "paper":
            raise PermissionError("paper autonomous campaign is forbidden outside paper")
        raise PermissionError("campaign authorization does not match active environment")
    if (
        settings.environment is TradingEnvironment.PROD
        and not settings.real_order_execution_enabled
    ):
        raise PermissionError("live autonomous campaign requires the real execution gate")
    return authorization


class AutonomousCampaignController:
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
        self.state_path = settings.autonomous_campaign_path.with_name(
            "autonomous_campaign_state.json"
        )

    async def run(self) -> None:
        interval = self.settings.autonomous_poll_interval_seconds
        while True:
            try:
                await self.tick(datetime.now(UTC))
            except Exception as exc:
                await self.repository.audit(
                    "AUTONOMOUS_CAMPAIGN_ERROR",
                    correlation_id="autonomous-campaign",
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
        state = self._load_state(authorization)
        reason = self._blocking_reason(authorization, now)
        if reason is not None:
            if reason != self._last_block_reason:
                await self.repository.audit(
                    "AUTONOMOUS_ENTRY_BLOCKED",
                    correlation_id=authorization.campaign_id,
                    payload={"reason": reason},
                )
                await self._notify_entry_paused(authorization, state, reason, now)
                self._last_block_reason = reason
            return None
        self._last_block_reason = None

        report = load_dashboard_report(self.settings.autonomous_report_path)
        if (
            report.strategy_status != "ACTIVE"
            or report.source_bar_interval_minutes != 1
            or report.analysis_bar_interval_minutes not in {10, 30, 60}
        ):
            await self._blocked(authorization, "REPORT_NOT_ACTIVE_INTRADAY")
            return None
        report_key = report.data_as_of.isoformat()
        expected_date = _latest_expected_report_date(now)
        if report.data_as_of.astimezone(KST).date() < expected_date:
            await self._blocked(authorization, "REPORT_STALE")
            return None
        if self.broker is None:
            await self._blocked(authorization, "LIVE_QUOTE_PROVIDER_UNAVAILABLE")
            return None

        overlay = self._load_intraday_overlay(report_key, now)
        if overlay is None:
            await self._blocked(authorization, "FRESH_INTRADAY_FLOW_OVERLAY_REQUIRED")
            return None
        overlay_revision = str(overlay["revision"]) if overlay is not None else None
        submission_key = (
            f"{report_key}|{overlay_revision}" if overlay_revision is not None else report_key
        )
        trading_day = now.astimezone(KST).date().isoformat()
        if submission_key in state.submitted_reports:
            return None
        if state.daily_batches.get(trading_day, 0) >= authorization.max_daily_batches:
            await self._blocked(authorization, "DAILY_BATCH_LIMIT")
            return None

        all_candidates = [*report.candidates, *report.extended_watchlist]
        safety_eligible = [
            candidate
            for candidate in all_candidates
            if candidate.context_status == "READY"
            and candidate.windows["14"].structure_status == "READY"
            and candidate.windows["14"].rank is not None
        ]
        preference = load_candidate_preference(self.settings, now)
        preference_order = (
            {symbol: index for index, symbol in enumerate(preference.symbols)}
            if preference is not None
            else {}
        )
        eligible = safety_eligible
        live_candidates: list[_LiveCandidate] = []
        overlay_rows: list[dict[str, object]] = []
        live_rows = (
            {str(row["symbol"]): row for row in overlay["rows"]}
            if overlay is not None
            else {}
        )
        for candidate in eligible:
            metrics = candidate.windows["14"]
            if metrics.box_low is None or metrics.box_high is None or metrics.rank is None:
                continue
            live_row = live_rows.get(candidate.code)
            if live_row is not None:
                live_price = live_row["live_price"]
                live_rank = live_row["live_rank_14d"]
                position_pct = live_row["live_position_pct"]
                discount_from_high_pct = live_row["discount_from_window_high_pct"]
                flow_status = live_row["intraday_flow_status"]
                combined_net_qty = live_row["intraday_combined_net_qty"]
                flow_strength_pct = live_row["intraday_flow_strength_pct"]
            else:
                continue
            # Fresh flow data is mandatory, but its sign is not an isolated
            # pass/fail rule. Outflow lowers the combined opportunity score;
            # account-wide market emergency gates remain authoritative.
            if flow_status != "READY":
                continue
            opportunity_score = _autonomous_opportunity_score(
                live_rank=live_rank,
                discount_from_high_pct=discount_from_high_pct,
                flow_strength_pct=flow_strength_pct,
            )
            live_candidates.append(
                _LiveCandidate(
                    live_rank=live_rank,
                    position_pct=position_pct,
                    candidate=candidate,
                    live_price=live_price,
                    discount_from_high_pct=discount_from_high_pct,
                    flow_strength_pct=flow_strength_pct,
                    opportunity_score=opportunity_score,
                )
            )
            overlay_rows.append(
                {
                    "symbol": candidate.code,
                    "name": candidate.name,
                    "base_price": str(candidate.current_price),
                    "live_price": live_price,
                    "live_position_pct": str(position_pct.quantize(Decimal("0.01"))),
                    "base_rank_14d": metrics.rank,
                    "live_rank_14d": live_rank,
                    "discount_from_window_high_pct": str(discount_from_high_pct),
                    "intraday_combined_net_qty": combined_net_qty,
                    "intraday_flow_strength_pct": str(flow_strength_pct),
                    "autonomous_opportunity_score": str(opportunity_score),
                }
            )
        # User preference remains first. Otherwise repeated-rise rank and price
        # discount carry 80%; current capital flow contributes 20% without
        # becoming a single-factor eligibility gate.
        live_candidates.sort(
            key=lambda item: (
                0 if item.candidate.code in preference_order else 1,
                preference_order.get(item.candidate.code, 999),
                -item.opportunity_score,
                -item.flow_strength_pct,
                -item.discount_from_high_pct,
                item.live_rank,
                item.position_pct,
            )
        )
        selected = live_candidates[: authorization.max_concurrent_positions]
        if not selected:
            await self._blocked(authorization, "NO_APPROVED_CANDIDATES")
            return None
        _atomic_json(
            self.settings.autonomous_campaign_path.with_name("autonomous_live_overlay.json"),
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
            candidate = live_item.candidate
            live_price = live_item.live_price
            metrics = candidate.windows["14"]
            if metrics.box_low is None or metrics.box_high is None or metrics.rank is None:
                continue
            selections.append(
                EntrySelection(
                    rank=live_item.live_rank,
                    symbol=candidate.code,
                    name=candidate.name,
                    entry_target_price_krw=live_price,
                    entry_price_source=(
                        "AUTONOMOUS_REPORT_PRICE"
                        if self.settings.environment is TradingEnvironment.PROD
                        else "PAPER_AUTONOMOUS_REPORT_PRICE"
                    ),
                    allocation_pct=allocation_pct,
                    selection_basis="QUANTITATIVE_OPPORTUNITY",
                    box_low=metrics.box_low,
                    box_high=metrics.box_high,
                )
            )
        if not selections:
            await self._blocked(authorization, "NO_VALID_SELECTIONS")
            return None
        # Quote collection may overlap a market-state transition. Re-check at
        # the durable command boundary instead of trusting the earlier state.
        reason = self._blocking_reason(authorization, now)
        if reason is not None:
            await self._blocked(authorization, reason)
            self._last_block_reason = reason
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
            request=(
                f"AUTONOMOUS_TRADING_CAMPAIGN:{authorization.campaign_id}"
                f":{overlay_revision or report_key}"
            ),
        )
        self.command_store.submit(mandate)
        state.submitted_reports = [*state.submitted_reports, submission_key][-100:]
        state.daily_batches[trading_day] = state.daily_batches.get(trading_day, 0) + 1
        state.updated_at = now
        _atomic_json(self.state_path, state.model_dump(mode="json"))
        await self.repository.audit(
            "AUTONOMOUS_MANDATE_SUBMITTED",
            correlation_id=authorization.campaign_id,
            payload={
                "command_id": mandate.command_id,
                "selection_version": AUTONOMOUS_SELECTION_VERSION,
                "report_data_as_of": report_key,
                "overlay_revision": overlay_revision,
                "symbols": [item.symbol for item in selections],
                "preferred_symbols": list(preference_order),
                "preferred_symbols_selected": [
                    item.symbol for item in selections if item.symbol in preference_order
                ],
                "preference_selection_policy": (
                    preference.selection_policy if preference is not None else None
                ),
            },
        )
        selected_positions = {
            item.candidate.code: item.position_pct for item in selected
        }
        await self._notify_selections(
            authorization,
            selections,
            selected_positions,
        )
        await self._notify_prices(authorization, selections)
        return mandate

    def _load_intraday_overlay(
        self,
        report_key: str,
        now: datetime,
    ) -> IntradayOverlayPayload | None:
        path = self.settings.intraday_overlay_path
        if not path.exists():
            return None
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            observed_at = datetime.fromisoformat(str(raw["observed_at"]))
            raw_rows = raw["rows"]
            if not isinstance(raw_rows, list):
                return None
            if observed_at.tzinfo is None:
                return None
            if str(raw["report_data_as_of"]) != report_key:
                return None
            rows: list[IntradayOverlayRow] = []
            for item in raw_rows:
                if not isinstance(item, dict) or not item.get("symbol"):
                    continue
                row = IntradayOverlayRow(
                    symbol=str(item["symbol"]),
                    live_rank_14d=int(str(item["live_rank_14d"])),
                    live_price=int(str(item["live_price"])),
                    live_position_pct=Decimal(str(item["live_position_pct"])),
                    discount_from_window_high_pct=Decimal(
                        str(item["discount_from_window_high_pct"])
                    ),
                    intraday_flow_status=str(item["intraday_flow_status"]),
                    intraday_combined_net_qty=int(
                        str(item["intraday_combined_net_qty"])
                    ),
                    intraday_flow_strength_pct=Decimal(
                        str(item["intraday_flow_strength_pct"])
                    ),
                )
                if (
                    len(row["symbol"]) != 6
                    or not row["symbol"].isdigit()
                    or not 1 <= row["live_rank_14d"] <= 200
                    or row["live_price"] <= 0
                ):
                    continue
                rows.append(row)
            symbols = {row["symbol"] for row in rows}
            if int(str(raw["coverage"])) < 180 or len(rows) < 180 or len(symbols) < 180:
                return None
            max_age = timedelta(seconds=self.settings.intraday_overlay_interval_seconds + 300)
            age = now.astimezone(UTC) - observed_at.astimezone(UTC)
            if age > max_age or age < -timedelta(minutes=2):
                return None
            return {"revision": str(raw["revision"]), "rows": rows}
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            return None

    def _blocking_reason(
        self,
        authorization: AutonomousCampaignAuthorization,
        now: datetime,
    ) -> str | None:
        if not authorization.permits_new_entries(now):
            return "CAMPAIGN_DISABLED_OR_EXPIRED"
        if self.settings.autonomous_kill_switch_path.exists():
            return "KILL_SWITCH_ACTIVE"
        if trading_session_phase(now) is not TradingSessionPhase.KRX_REGULAR:
            return "OUTSIDE_REGULAR_SESSION"
        if self.command_store.load_active() is not None:
            return "ACTIVE_MANUAL_OR_AUTONOMOUS_COMMAND"
        if self.core.positions or self.core.submitted:
            return "ACCOUNT_NOT_FLAT"
        if self.core.orchestrator.state is not OrchestratorState.RUNNING:
            return "ORCHESTRATOR_NOT_READY"
        if not getattr(self.core, "market_guard_initialized", True):
            return "MARKET_GUARD_NOT_INITIALIZED"
        if self.core.market_entry_resume_required:
            return "MARKET_RESUME_CONFIRMATION_REQUIRED"
        if self.core.market_risk is not MarketRisk.NORMAL:
            return "MARKET_RISK_NOT_NORMAL"
        return None

    async def _blocked(
        self,
        authorization: AutonomousCampaignAuthorization,
        reason: str,
    ) -> None:
        await self.repository.audit(
            "AUTONOMOUS_ENTRY_BLOCKED",
            correlation_id=authorization.campaign_id,
            payload={"reason": reason},
        )
        return None

    async def _notify_prices(
        self,
        authorization: AutonomousCampaignAuthorization,
        selections: list[EntrySelection],
    ) -> None:
        if self.notifier is None:
            await self.repository.audit(
                "AUTONOMOUS_PRICE_EMAIL_SKIPPED",
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
                    "AUTONOMOUS_PRICE_EMAIL_SENT",
                    correlation_id=authorization.campaign_id,
                    payload={
                        "symbols": [selection.symbol for selection in selections],
                        "recipient_count": receipt.recipient_count,
                    },
                )
                return
            except (NotificationError, OSError) as exc:
                await self.repository.audit(
                    "AUTONOMOUS_PRICE_EMAIL_ERROR",
                    correlation_id=authorization.campaign_id,
                    payload={
                        "attempt": attempt + 1,
                        "error": type(exc).__name__,
                    },
                )
                if attempt < len(retry_delays):
                    await asyncio.sleep(retry_delays[attempt])

    async def _notify_entry_paused(
        self,
        authorization: AutonomousCampaignAuthorization,
        state: AutonomousCampaignState,
        reason: str,
        now: datetime,
    ) -> None:
        notify_reasons = {
            "MARKET_RESUME_CONFIRMATION_REQUIRED",
            "MARKET_RISK_NOT_NORMAL",
            "KILL_SWITCH_ACTIVE",
            "ORCHESTRATOR_NOT_READY",
        }
        if reason not in notify_reasons:
            return
        notification_key = f"{now.astimezone(KST).date().isoformat()}:ENTRY_PAUSED"
        if notification_key in state.notification_keys:
            return
        if self.notifier is None:
            await self.repository.audit(
                "AUTONOMOUS_ENTRY_PAUSED_EMAIL_SKIPPED",
                correlation_id=authorization.campaign_id,
                payload={"reason": "SMTP_DISABLED_OR_UNAVAILABLE"},
            )
            return
        retry_delays = (2.0, 10.0)
        for attempt in range(3):
            try:
                receipt = await asyncio.to_thread(
                    self.notifier.send_autonomous_entry_paused,
                    reason=reason,
                    dashboard_url=self.settings.market_dashboard_public_url,
                )
                state.notification_keys.append(notification_key)
                state.updated_at = now
                _atomic_json(self.state_path, state.model_dump(mode="json"))
                await self.repository.audit(
                    "AUTONOMOUS_ENTRY_PAUSED_EMAIL_SENT",
                    correlation_id=authorization.campaign_id,
                    payload={
                        "reason": reason,
                        "recipient_count": receipt.recipient_count,
                    },
                )
                return
            except (NotificationError, OSError) as exc:
                await self.repository.audit(
                    "AUTONOMOUS_ENTRY_PAUSED_EMAIL_ERROR",
                    correlation_id=authorization.campaign_id,
                    payload={
                        "reason": reason,
                        "attempt": attempt + 1,
                        "error": type(exc).__name__,
                    },
                )
                if attempt < len(retry_delays):
                    await asyncio.sleep(retry_delays[attempt])

    async def _notify_selections(
        self,
        authorization: AutonomousCampaignAuthorization,
        selections: list[EntrySelection],
        selected_positions: dict[str, Decimal],
    ) -> None:
        if self.notifier is None:
            await self.repository.audit(
                "AUTONOMOUS_SELECTION_EMAIL_SKIPPED",
                correlation_id=authorization.campaign_id,
                payload={"reason": "SMTP_DISABLED_OR_UNAVAILABLE"},
            )
            return
        rows = [
            (
                selection.name,
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
                    "AUTONOMOUS_SELECTION_EMAIL_SENT",
                    correlation_id=authorization.campaign_id,
                    payload={
                        "symbols": [selection.symbol for selection in selections],
                        "recipient_count": receipt.recipient_count,
                    },
                )
                return
            except (NotificationError, OSError) as exc:
                await self.repository.audit(
                    "AUTONOMOUS_SELECTION_EMAIL_ERROR",
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
        authorization: AutonomousCampaignAuthorization,
    ) -> AutonomousCampaignState:
        if not self.state_path.exists():
            return AutonomousCampaignState(
                campaign_id=authorization.campaign_id,
                updated_at=datetime.now(UTC),
            )
        state = AutonomousCampaignState.model_validate_json(
            self.state_path.read_text(encoding="utf-8")
        )
        if state.campaign_id != authorization.campaign_id:
            return AutonomousCampaignState(
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


# Archived paper test/import compatibility. Production uses the neutral names.
PaperAutonomousCampaignAuthorization = AutonomousCampaignAuthorization
PaperAutonomousCampaignState = AutonomousCampaignState
PaperAutonomousCampaignController = AutonomousCampaignController
