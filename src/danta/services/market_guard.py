from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from danta.domain.market import MarketRisk


@dataclass(frozen=True, slots=True)
class MarketGuardPolicy:
    version: str = "kospi-market-guard-paper-v1"
    caution_return_pct: Decimal = Decimal("-1.5")
    risk_off_return_pct: Decimal = Decimal("-2.5")
    caution_decline_ratio: Decimal = Decimal("0.65")
    risk_off_decline_ratio: Decimal = Decimal("0.80")
    confirmation_samples: int = 3


@dataclass(frozen=True, slots=True)
class MarketGuardObservation:
    kospi_return_pct: Decimal
    declining_issue_ratio: Decimal


@dataclass(frozen=True, slots=True)
class MarketGuardDecision:
    risk: MarketRisk
    stress_score: Decimal
    reason_codes: tuple[str, ...]


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
        if (
            value.kospi_return_pct <= self.policy.risk_off_return_pct
            and value.declining_issue_ratio >= self.policy.risk_off_decline_ratio
        ):
            instantaneous = MarketRisk.RISK_OFF
            reasons: tuple[str, ...] = (
                "KOSPI_SHARP_DROP",
                "MARKET_BREADTH_COLLAPSE",
            )
        elif (
            value.kospi_return_pct <= self.policy.caution_return_pct
            or value.declining_issue_ratio >= self.policy.caution_decline_ratio
        ):
            instantaneous = MarketRisk.CAUTION
            reasons = ("MARKET_STRESS_ELEVATED",)
        else:
            instantaneous = MarketRisk.NORMAL
            reasons = ("MARKET_NORMAL",)
        self._history.append(instantaneous)
        confirmed = instantaneous
        if instantaneous is MarketRisk.RISK_OFF and (
            len(self._history) < self.policy.confirmation_samples
            or any(item is not MarketRisk.RISK_OFF for item in self._history)
        ):
            confirmed = MarketRisk.CAUTION
            reasons += ("RISK_OFF_AWAITING_CONFIRMATION",)
        return MarketGuardDecision(
            risk=confirmed,
            stress_score=_stress(value),
            reason_codes=reasons,
        )


def _stress(value: MarketGuardObservation) -> Decimal:
    return min(
        Decimal("1"),
        max(
            Decimal("0"),
            (
                max(Decimal("0"), -value.kospi_return_pct) / Decimal("5")
                + value.declining_issue_ratio
            )
            / Decimal("2"),
        ),
    )
