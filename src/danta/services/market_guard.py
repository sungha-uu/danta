from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from danta.domain.market import MarketRisk
from danta.domain.market_wide import MarketWideRiskLevel


@dataclass(frozen=True, slots=True)
class MarketGuardPolicy:
    version: str = "kospi-market-guard-paper-v2"
    caution_return_pct: Decimal = Decimal("-1.5")
    risk_off_return_pct: Decimal = Decimal("-2.5")
    panic_return_pct: Decimal = Decimal("-5.0")
    caution_decline_ratio: Decimal = Decimal("0.65")
    risk_off_decline_ratio: Decimal = Decimal("0.80")
    caution_foreign_net_ratio: Decimal = Decimal("-0.01")
    risk_off_foreign_net_ratio: Decimal = Decimal("-0.02")
    confirmation_samples: int = 3


@dataclass(frozen=True, slots=True)
class MarketGuardObservation:
    kospi_return_pct: Decimal
    declining_issue_ratio: Decimal
    foreign_net_ratio: Decimal = Decimal("0")
    foreign_delta_5m: int = 0
    pension_net_million: int = 0
    program_net_million: int = 0
    program_delta_5m: int = 0
    provider_complete: bool = True
    market_emergency: bool = False


@dataclass(frozen=True, slots=True)
class MarketGuardDecision:
    risk: MarketRisk
    stress_score: Decimal
    reason_codes: tuple[str, ...]
    level: MarketWideRiskLevel = MarketWideRiskLevel.NORMAL


class MarketRegimeGuard:
    """Local, deterministic account-wide new-entry circuit breaker."""

    def __init__(self, policy: MarketGuardPolicy | None = None) -> None:
        self.policy = policy or MarketGuardPolicy()
        self._history: deque[MarketRisk] = deque(
            maxlen=self.policy.confirmation_samples
        )

    def observe(self, value: MarketGuardObservation) -> MarketGuardDecision:
        if value.declining_issue_ratio < 0 or value.declining_issue_ratio > 1:
            raise ValueError("declining_issue_ratio must be between 0 and 1")
        reasons: tuple[str, ...]
        if not value.provider_complete:
            instantaneous = MarketRisk.RISK_OFF
            level = MarketWideRiskLevel.RISK_OFF
            reasons = ("MARKET_DATA_INCOMPLETE",)
        elif value.market_emergency or (
            value.kospi_return_pct <= self.policy.panic_return_pct
            and value.declining_issue_ratio >= self.policy.risk_off_decline_ratio
        ):
            instantaneous = MarketRisk.RISK_OFF
            level = MarketWideRiskLevel.PANIC
            reasons = (
                "MARKET_EMERGENCY",
                "KOSPI_PANIC_DROP",
                "MARKET_BREADTH_COLLAPSE",
            )
        elif (
            value.kospi_return_pct <= self.policy.risk_off_return_pct
            and value.declining_issue_ratio >= self.policy.risk_off_decline_ratio
        ) or (
            value.kospi_return_pct <= self.policy.caution_return_pct
            and value.declining_issue_ratio >= self.policy.caution_decline_ratio
            and value.foreign_net_ratio <= self.policy.risk_off_foreign_net_ratio
        ):
            instantaneous = MarketRisk.RISK_OFF
            level = MarketWideRiskLevel.RISK_OFF
            reasons = (
                "KOSPI_SHARP_DROP",
                "MARKET_BREADTH_COLLAPSE",
            )
        elif (
            value.kospi_return_pct <= self.policy.caution_return_pct
            or value.declining_issue_ratio >= self.policy.caution_decline_ratio
            or value.foreign_net_ratio <= self.policy.caution_foreign_net_ratio
        ):
            instantaneous = MarketRisk.CAUTION
            level = MarketWideRiskLevel.CAUTION
            reasons = ("MARKET_STRESS_ELEVATED",)
        else:
            instantaneous = MarketRisk.NORMAL
            level = MarketWideRiskLevel.NORMAL
            reasons = ("MARKET_NORMAL",)
        if (
            value.foreign_net_ratio < 0
            and value.pension_net_million > 0
            and value.kospi_return_pct < 0
        ):
            reasons += ("FOREIGN_OUTFLOW_PENSION_ABSORPTION_PROXY",)
        if value.program_net_million < 0 and value.program_delta_5m < 0:
            reasons += ("PROGRAM_SELLING_ACCELERATING",)
        self._history.append(instantaneous)
        confirmed = instantaneous
        if (
            instantaneous is MarketRisk.RISK_OFF
            and level is not MarketWideRiskLevel.PANIC
            and value.provider_complete
            and (
                len(self._history) < self.policy.confirmation_samples
                or any(item is not MarketRisk.RISK_OFF for item in self._history)
            )
        ):
            confirmed = MarketRisk.CAUTION
            level = MarketWideRiskLevel.CAUTION
            reasons += ("RISK_OFF_AWAITING_CONFIRMATION",)
        return MarketGuardDecision(
            risk=confirmed,
            stress_score=_stress(value),
            reason_codes=reasons,
            level=level,
        )


def _stress(value: MarketGuardObservation) -> Decimal:
    flow_stress = min(
        Decimal("1"),
        max(Decimal("0"), -value.foreign_net_ratio / Decimal("0.03")),
    )
    return min(
        Decimal("1"),
        max(
            Decimal("0"),
            (
                max(Decimal("0"), -value.kospi_return_pct) / Decimal("5")
                + value.declining_issue_ratio
                + flow_stress
            )
            / Decimal("3"),
        ),
    )
