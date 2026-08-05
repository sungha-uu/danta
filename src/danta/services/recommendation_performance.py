from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from danta.adapters.kis.client import KisMinuteBar
from danta.dashboard.models import AiGrade, CandidateView, DashboardReport
from danta.services.intraday_report import MinuteBarStore

KST = timezone(timedelta(hours=9))
HUNDRED = Decimal("100")
HORIZONS = (1, 3, 5)
TARGETS = {
    "plus_5": Decimal("5"),
    "plus_6": Decimal("6"),
    "plus_10": Decimal("10"),
}
STOPS = {
    "minus_3": Decimal("-3"),
    "minus_5": Decimal("-5"),
    "minus_7": Decimal("-7"),
}
RECOMMENDED_GRADES = frozenset({"STRONG_RECOMMEND", "RECOMMEND"})


class RecommendationPerformanceError(RuntimeError):
    """Raised when a frozen recommendation audit cannot be trusted."""


class HorizonPerformance(BaseModel):
    horizon_trading_days: Literal[1, 3, 5]
    status: Literal["PENDING", "COMPLETE"]
    observed_trading_days: int = Field(ge=0, le=5)
    end_date: str | None = None
    close_return_pct: Decimal | None = None
    net_close_return_pct: Decimal | None = None
    mfe_pct: Decimal | None = None
    mae_pct: Decimal | None = None
    first_hits: dict[str, str | None] = Field(default_factory=dict)
    plus_6_before_minus_7: bool | None = None


class RecommendationObservation(BaseModel):
    symbol: str
    name: str
    rank_14d: int = Field(ge=1, le=50)
    grade: AiGrade
    ai_score: Decimal | None = None
    ai_comment: str
    reference_price: Decimal = Field(gt=0)
    autonomous_eligible: bool = False
    setup_type: Literal["LOWER_REVERSAL", "REPEAT_CONTINUATION"] | None = None
    autonomous_rejection_reasons: list[str] = Field(default_factory=list)
    outcomes: dict[str, HorizonPerformance] = Field(default_factory=dict)


class RecommendationSnapshot(BaseModel):
    schema_version: Literal["recommendation-performance-v1"] = (
        "recommendation-performance-v1"
    )
    report_date: str
    report_data_as_of: datetime
    frozen_at: datetime
    updated_at: datetime
    market_regime: str
    calculation_version: str
    model_id: str
    prompt_version: str
    round_trip_cost_bps: Decimal = Field(ge=0)
    decision_fingerprint: str
    observations: list[RecommendationObservation] = Field(min_length=50, max_length=50)


class GroupPerformance(BaseModel):
    group: str
    horizon_trading_days: Literal[1, 3, 5]
    completed_samples: int = Field(ge=0)
    mean_close_return_pct: Decimal | None = None
    mean_net_close_return_pct: Decimal | None = None
    mean_mfe_pct: Decimal | None = None
    mean_mae_pct: Decimal | None = None
    plus_5_hit_rate_pct: Decimal | None = None
    plus_6_hit_rate_pct: Decimal | None = None
    plus_10_hit_rate_pct: Decimal | None = None
    minus_7_hit_rate_pct: Decimal | None = None
    plus_6_before_minus_7_rate_pct: Decimal | None = None


class MissedWinner(BaseModel):
    report_date: str
    symbol: str
    name: str
    rank_14d: int = Field(ge=1, le=50)
    grade: AiGrade
    mfe_pct: Decimal
    mae_pct: Decimal
    close_return_pct: Decimal
    rejection_reasons: list[str] = Field(default_factory=list)


class RecommendationPerformanceSummary(BaseModel):
    schema_version: Literal["recommendation-performance-summary-v1"] = (
        "recommendation-performance-summary-v1"
    )
    generated_at: datetime
    evaluation_cutoff_date: str
    round_trip_cost_bps: Decimal
    snapshot_count: int = Field(ge=0)
    frozen_observation_count: int = Field(ge=0)
    completed_outcome_count: int = Field(ge=0)
    recommendation_edge_status: Literal[
        "INSUFFICIENT_SAMPLE",
        "EDGE_OBSERVED",
        "EDGE_NOT_CONFIRMED",
    ]
    minimum_samples_per_comparison_group: int = 30
    groups: list[GroupPerformance]
    missed_plus_6_count_1d: int = Field(default=0, ge=0)
    top_missed_winners_1d: list[MissedWinner] = Field(default_factory=list)
    policy_note: str = (
        "성과는 추천 모델 개선 근거일 뿐 운영 등급·임계값을 자동 변경하지 않는다."
    )


@dataclass(frozen=True, slots=True)
class PerformanceUpdateResult:
    snapshot_path: Path
    summary_path: Path
    snapshot_count: int
    frozen_observation_count: int
    completed_outcome_count: int
    recommendation_edge_status: str


class RecommendationPerformanceTracker:
    """Freeze daily top-50 decisions and evaluate them on later minute bars."""

    def __init__(
        self,
        root: Path,
        *,
        round_trip_cost_bps: Decimal = Decimal("35"),
        minimum_samples_per_group: int = 30,
    ) -> None:
        if round_trip_cost_bps < 0:
            raise ValueError("round_trip_cost_bps must not be negative")
        if minimum_samples_per_group < 1:
            raise ValueError("minimum_samples_per_group must be positive")
        self.root = root
        self.snapshot_root = root / "snapshots"
        self.summary_path = root / "latest-summary.json"
        self.round_trip_cost_bps = round_trip_cost_bps
        self.minimum_samples_per_group = minimum_samples_per_group

    def update(
        self,
        report: DashboardReport,
        store: MinuteBarStore,
        *,
        now: datetime | None = None,
    ) -> PerformanceUpdateResult:
        timestamp = (now or datetime.now(KST)).astimezone(KST)
        cutoff = report.data_as_of.astimezone(KST).date()
        candidates = _validated_top_50(report)
        fingerprint = _decision_fingerprint(report, candidates)
        snapshot_path = self.snapshot_root / f"{cutoff.isoformat()}.json"
        if snapshot_path.exists():
            current = RecommendationSnapshot.model_validate_json(
                snapshot_path.read_text(encoding="utf-8")
            )
            if current.decision_fingerprint != fingerprint:
                raise RecommendationPerformanceError(
                    "a frozen recommendation snapshot cannot be overwritten"
                )
        else:
            current = RecommendationSnapshot(
                report_date=cutoff.isoformat(),
                report_data_as_of=report.data_as_of,
                frozen_at=timestamp,
                updated_at=timestamp,
                market_regime=report.market_regime,
                calculation_version=report.calculation_version,
                model_id=report.model_id,
                prompt_version=report.prompt_version,
                round_trip_cost_bps=self.round_trip_cost_bps,
                decision_fingerprint=fingerprint,
                observations=[
                    _freeze_observation(candidate) for candidate in candidates
                ],
            )
            _write_model(snapshot_path, current)

        snapshots = self._load_snapshots()
        updated: list[RecommendationSnapshot] = []
        for snapshot in snapshots:
            evaluated = self._evaluate_snapshot(
                snapshot,
                store,
                cutoff_date=cutoff.isoformat(),
                updated_at=timestamp,
            )
            _write_model(
                self.snapshot_root / f"{evaluated.report_date}.json",
                evaluated,
            )
            updated.append(evaluated)

        summary = _summarize(
            updated,
            generated_at=timestamp,
            cutoff_date=cutoff.isoformat(),
            round_trip_cost_bps=self.round_trip_cost_bps,
            minimum_samples_per_group=self.minimum_samples_per_group,
        )
        _write_model(self.summary_path, summary)
        return PerformanceUpdateResult(
            snapshot_path=snapshot_path,
            summary_path=self.summary_path,
            snapshot_count=summary.snapshot_count,
            frozen_observation_count=summary.frozen_observation_count,
            completed_outcome_count=summary.completed_outcome_count,
            recommendation_edge_status=summary.recommendation_edge_status,
        )

    def _load_snapshots(self) -> list[RecommendationSnapshot]:
        if not self.snapshot_root.exists():
            return []
        snapshots = [
            RecommendationSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.snapshot_root.glob("*.json"))
        ]
        if any(
            item.round_trip_cost_bps != self.round_trip_cost_bps
            for item in snapshots
        ):
            raise RecommendationPerformanceError(
                "round-trip cost assumption changed; start a versioned audit series"
            )
        return snapshots

    def _evaluate_snapshot(
        self,
        snapshot: RecommendationSnapshot,
        store: MinuteBarStore,
        *,
        cutoff_date: str,
        updated_at: datetime,
    ) -> RecommendationSnapshot:
        observations: list[RecommendationObservation] = []
        for observation in snapshot.observations:
            future_dates = _future_trading_dates(
                store,
                observation.symbol,
                after=snapshot.report_date,
                through=cutoff_date,
            )
            outcomes = {
                str(horizon): _evaluate_horizon(
                    store,
                    observation.symbol,
                    future_dates[:horizon],
                    horizon=horizon,
                    reference_price=observation.reference_price,
                    round_trip_cost_bps=snapshot.round_trip_cost_bps,
                )
                for horizon in HORIZONS
            }
            observations.append(observation.model_copy(update={"outcomes": outcomes}))
        return snapshot.model_copy(
            update={
                "updated_at": updated_at,
                "observations": observations,
            }
        )


def _validated_top_50(report: DashboardReport) -> list[CandidateView]:
    ranked = sorted(
        [*report.candidates, *report.extended_watchlist],
        key=lambda item: item.windows["14"].rank or 999,
    )
    selected = [
        candidate
        for candidate in ranked
        if candidate.windows["14"].rank is not None
        and candidate.windows["14"].rank <= 50
    ]
    if len(selected) != 50:
        raise RecommendationPerformanceError(
            f"recommendation audit requires exactly 50 ranked candidates, got {len(selected)}"
        )
    for candidate in selected:
        metrics = candidate.windows["14"]
        if metrics.structure_status != "READY":
            raise RecommendationPerformanceError(
                f"14-day structure is not ready: {candidate.code}"
            )
        if metrics.ai_grade is None or metrics.ai_comment is None:
            raise RecommendationPerformanceError(
                f"AI review is missing for top-50 candidate: {candidate.code}"
            )
    return selected


def _freeze_observation(candidate: CandidateView) -> RecommendationObservation:
    metrics = candidate.windows["14"]
    if metrics.rank is None or metrics.ai_grade is None or metrics.ai_comment is None:
        raise RecommendationPerformanceError("candidate review is incomplete")
    setup_type, rejection_reasons = _autonomous_setup(candidate)
    return RecommendationObservation(
        symbol=candidate.code,
        name=candidate.name,
        rank_14d=metrics.rank,
        grade=metrics.ai_grade,
        ai_score=metrics.ai_score,
        ai_comment=metrics.ai_comment,
        reference_price=candidate.current_price,
        autonomous_eligible=(
            metrics.ai_grade in RECOMMENDED_GRADES and setup_type is not None
        ),
        setup_type=setup_type,
        autonomous_rejection_reasons=rejection_reasons,
    )


def _autonomous_setup(
    candidate: CandidateView,
) -> tuple[
    Literal["LOWER_REVERSAL", "REPEAT_CONTINUATION"] | None,
    list[str],
]:
    metrics = candidate.windows["14"]
    reasons: list[str] = []
    if metrics.ai_grade not in RECOMMENDED_GRADES:
        reasons.append("AI_GRADE_NOT_APPROVED")
    lower_reversal = (
        (metrics.target_reach_count or 0) >= 1
        and metrics.position_pct is not None
        and metrics.position_pct <= Decimal("35")
        and metrics.current_vs_window_high_pct is not None
        and metrics.current_vs_window_high_pct
        <= (Decimal("1") / Decimal("1.10") - Decimal("1")) * HUNDRED
        and metrics.target_price_10pct is not None
        and metrics.target_price_10pct > candidate.current_price
        and metrics.decline_shape != "STRUCTURAL_DECLINE"
    )
    continuation = (
        metrics.rank is not None
        and metrics.rank <= 20
        and (metrics.target_reach_count or 0) >= 1
        and metrics.position_pct is not None
        and metrics.position_pct <= Decimal("75")
        and metrics.current_vs_window_high_pct is not None
        and metrics.current_vs_window_high_pct <= Decimal("-8")
        and (metrics.average_up_swing_pct or Decimal("0")) >= Decimal("10")
        and (metrics.up_swing_count or 0) >= 5
        and metrics.average_time_to_6pct_hours is not None
        and metrics.average_time_to_6pct_hours <= Decimal("6")
        and metrics.volume_ratio >= Decimal("0.75")
        and metrics.target_price_10pct is not None
        and metrics.target_price_10pct <= candidate.current_price
        and metrics.decline_shape != "STRUCTURAL_DECLINE"
    )
    if lower_reversal:
        return "LOWER_REVERSAL", reasons
    if continuation:
        return "REPEAT_CONTINUATION", reasons
    if metrics.position_pct is not None and metrics.position_pct > Decimal("75"):
        reasons.append("CURRENT_POSITION_ABOVE_75")
    if metrics.target_price_10pct is not None and (
        metrics.target_price_10pct <= candidate.current_price
    ):
        reasons.append("FIRST_10PCT_TARGET_ALREADY_PASSED")
    if metrics.current_vs_window_high_pct is not None and (
        metrics.current_vs_window_high_pct > Decimal("-8")
    ):
        reasons.append("WINDOW_HIGH_SPACE_BELOW_8PCT")
    if metrics.decline_shape == "STRUCTURAL_DECLINE":
        reasons.append("STRUCTURAL_DECLINE")
    if (metrics.up_swing_count or 0) < 5:
        reasons.append("REPEAT_RISE_COUNT_BELOW_5")
    return None, reasons


def _decision_fingerprint(
    report: DashboardReport,
    candidates: list[CandidateView],
) -> str:
    payload = {
        "report_data_as_of": report.data_as_of.isoformat(),
        "calculation_version": report.calculation_version,
        "model_id": report.model_id,
        "prompt_version": report.prompt_version,
        "observations": [
            _freeze_observation(candidate).model_dump(
                mode="json",
                # Audit-only diagnostics may be added without mutating the
                # frozen recommendation decision or its fingerprint.
                exclude={
                    "outcomes",
                    "autonomous_eligible",
                    "setup_type",
                    "autonomous_rejection_reasons",
                },
            )
            for candidate in candidates
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _future_trading_dates(
    store: MinuteBarStore,
    symbol: str,
    *,
    after: str,
    through: str,
) -> list[str]:
    symbol_root = store.root / symbol
    if not symbol_root.exists():
        return []
    return [
        path.stem
        for path in sorted(symbol_root.glob("*.json"))
        if after.replace("-", "") < path.stem <= through.replace("-", "")
        and store.is_complete(symbol, path.stem)
    ]


def _evaluate_horizon(
    store: MinuteBarStore,
    symbol: str,
    trading_dates: list[str],
    *,
    horizon: int,
    reference_price: Decimal,
    round_trip_cost_bps: Decimal,
) -> HorizonPerformance:
    bars = [
        bar
        for trading_date in trading_dates
        for bar in store.load(symbol, trading_date)
    ]
    complete = len(trading_dates) >= horizon
    if not bars:
        return HorizonPerformance(
            horizon_trading_days=horizon,  # type: ignore[arg-type]
            status="PENDING",
            observed_trading_days=0,
            first_hits={**dict.fromkeys(TARGETS), **dict.fromkeys(STOPS)},
        )
    bars.sort(key=lambda item: (item.trading_date, item.trading_time))
    first_hits = {
        name: _first_hit(bars, reference_price, threshold)
        for name, threshold in {**TARGETS, **STOPS}.items()
    }
    close_return = _return_pct(Decimal(bars[-1].close), reference_price)
    mfe = _return_pct(Decimal(max(bar.high for bar in bars)), reference_price)
    mae = _return_pct(Decimal(min(bar.low for bar in bars)), reference_price)
    plus_6 = first_hits["plus_6"]
    minus_7 = first_hits["minus_7"]
    ordered: bool | None = None
    if plus_6 is not None or minus_7 is not None:
        ordered = plus_6 is not None and (
            minus_7 is None or plus_6 < minus_7
        )
    return HorizonPerformance(
        horizon_trading_days=horizon,  # type: ignore[arg-type]
        status="COMPLETE" if complete else "PENDING",
        observed_trading_days=len(trading_dates),
        end_date=trading_dates[-1],
        close_return_pct=_quantize(close_return),
        net_close_return_pct=_quantize(
            close_return - round_trip_cost_bps / HUNDRED
        ),
        mfe_pct=_quantize(mfe),
        mae_pct=_quantize(mae),
        first_hits=first_hits,
        plus_6_before_minus_7=ordered,
    )


def _first_hit(
    bars: list[KisMinuteBar],
    reference_price: Decimal,
    threshold_pct: Decimal,
) -> str | None:
    target = reference_price * (Decimal("1") + threshold_pct / HUNDRED)
    for bar in bars:
        reached = (
            Decimal(bar.high) >= target
            if threshold_pct > 0
            else Decimal(bar.low) <= target
        )
        if reached:
            value = f"{bar.trading_date}{bar.trading_time}"
            return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
                tzinfo=KST
            ).isoformat()
    return None


def _return_pct(price: Decimal, reference_price: Decimal) -> Decimal:
    return (price / reference_price - Decimal("1")) * HUNDRED


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _summarize(
    snapshots: list[RecommendationSnapshot],
    *,
    generated_at: datetime,
    cutoff_date: str,
    round_trip_cost_bps: Decimal,
    minimum_samples_per_group: int,
) -> RecommendationPerformanceSummary:
    groups: list[GroupPerformance] = []
    for horizon in HORIZONS:
        complete = [
            (observation, observation.outcomes.get(str(horizon)))
            for snapshot in snapshots
            for observation in snapshot.observations
            if observation.outcomes.get(str(horizon)) is not None
            and observation.outcomes[str(horizon)].status == "COMPLETE"
        ]
        for grade in (
            "STRONG_RECOMMEND",
            "RECOMMEND",
            "NOT_RECOMMEND",
            "STRONG_NOT_RECOMMEND",
        ):
            groups.append(
                _group_summary(
                    grade,
                    horizon,
                    [
                        outcome
                        for observation, outcome in complete
                        if observation.grade == grade and outcome is not None
                    ],
                )
            )
        groups.append(
            _group_summary(
                "RECOMMENDED",
                horizon,
                [
                    outcome
                    for observation, outcome in complete
                    if observation.grade in RECOMMENDED_GRADES
                    and outcome is not None
                ],
            )
        )
        groups.append(
            _group_summary(
                "NOT_RECOMMENDED",
                horizon,
                [
                    outcome
                    for observation, outcome in complete
                    if observation.grade not in RECOMMENDED_GRADES
                    and outcome is not None
                ],
            )
        )
    edge_status = _edge_status(groups, minimum_samples_per_group)
    completed_count = sum(
        outcome.status == "COMPLETE"
        for snapshot in snapshots
        for observation in snapshot.observations
        for outcome in observation.outcomes.values()
    )
    missed_winners = sorted(
        [
            MissedWinner(
                report_date=snapshot.report_date,
                symbol=observation.symbol,
                name=observation.name,
                rank_14d=observation.rank_14d,
                grade=observation.grade,
                mfe_pct=outcome.mfe_pct,
                mae_pct=outcome.mae_pct,
                close_return_pct=outcome.close_return_pct,
                rejection_reasons=(
                    observation.autonomous_rejection_reasons
                    or ["AI_GRADE_NOT_APPROVED"]
                ),
            )
            for snapshot in snapshots
            for observation in snapshot.observations
            for outcome in [observation.outcomes.get("1")]
            if observation.grade not in RECOMMENDED_GRADES
            and outcome is not None
            and outcome.status == "COMPLETE"
            and outcome.mfe_pct is not None
            and outcome.mae_pct is not None
            and outcome.close_return_pct is not None
            and outcome.mfe_pct >= Decimal("6")
        ],
        key=lambda item: (-item.mfe_pct, item.report_date, item.rank_14d),
    )
    return RecommendationPerformanceSummary(
        generated_at=generated_at,
        evaluation_cutoff_date=cutoff_date,
        round_trip_cost_bps=round_trip_cost_bps,
        snapshot_count=len(snapshots),
        frozen_observation_count=sum(
            len(snapshot.observations) for snapshot in snapshots
        ),
        completed_outcome_count=completed_count,
        recommendation_edge_status=edge_status,
        minimum_samples_per_comparison_group=minimum_samples_per_group,
        groups=groups,
        missed_plus_6_count_1d=len(missed_winners),
        top_missed_winners_1d=missed_winners[:50],
    )


def _group_summary(
    group: str,
    horizon: int,
    outcomes: list[HorizonPerformance],
) -> GroupPerformance:
    if not outcomes:
        return GroupPerformance(
            group=group,
            horizon_trading_days=horizon,  # type: ignore[arg-type]
            completed_samples=0,
        )
    return GroupPerformance(
        group=group,
        horizon_trading_days=horizon,  # type: ignore[arg-type]
        completed_samples=len(outcomes),
        mean_close_return_pct=_mean(
            [item.close_return_pct for item in outcomes]
        ),
        mean_net_close_return_pct=_mean(
            [item.net_close_return_pct for item in outcomes]
        ),
        mean_mfe_pct=_mean([item.mfe_pct for item in outcomes]),
        mean_mae_pct=_mean([item.mae_pct for item in outcomes]),
        plus_5_hit_rate_pct=_hit_rate(outcomes, "plus_5"),
        plus_6_hit_rate_pct=_hit_rate(outcomes, "plus_6"),
        plus_10_hit_rate_pct=_hit_rate(outcomes, "plus_10"),
        minus_7_hit_rate_pct=_hit_rate(outcomes, "minus_7"),
        plus_6_before_minus_7_rate_pct=_boolean_rate(
            [item.plus_6_before_minus_7 for item in outcomes]
        ),
    )


def _mean(values: list[Decimal | None]) -> Decimal | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return _quantize(sum(present, Decimal("0")) / Decimal(len(present)))


def _hit_rate(outcomes: list[HorizonPerformance], key: str) -> Decimal:
    hits = sum(item.first_hits.get(key) is not None for item in outcomes)
    return _quantize(Decimal(hits) / Decimal(len(outcomes)) * HUNDRED)


def _boolean_rate(values: list[bool | None]) -> Decimal | None:
    decided = [value for value in values if value is not None]
    if not decided:
        return None
    return _quantize(
        Decimal(sum(decided)) / Decimal(len(decided)) * HUNDRED
    )


def _edge_status(
    groups: list[GroupPerformance],
    minimum_samples_per_group: int,
) -> Literal[
    "INSUFFICIENT_SAMPLE",
    "EDGE_OBSERVED",
    "EDGE_NOT_CONFIRMED",
]:
    recommended = next(
        item
        for item in groups
        if item.group == "RECOMMENDED" and item.horizon_trading_days == 5
    )
    baseline = next(
        item
        for item in groups
        if item.group == "NOT_RECOMMENDED" and item.horizon_trading_days == 5
    )
    if (
        recommended.completed_samples < minimum_samples_per_group
        or baseline.completed_samples < minimum_samples_per_group
    ):
        return "INSUFFICIENT_SAMPLE"
    if (
        recommended.plus_6_hit_rate_pct is not None
        and baseline.plus_6_hit_rate_pct is not None
        and recommended.mean_net_close_return_pct is not None
        and baseline.mean_net_close_return_pct is not None
        and recommended.plus_6_hit_rate_pct > baseline.plus_6_hit_rate_pct
        and recommended.mean_net_close_return_pct
        > baseline.mean_net_close_return_pct
    ):
        return "EDGE_OBSERVED"
    return "EDGE_NOT_CONFIRMED"


def _write_model(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
