from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class MarketWideRiskLevel(StrEnum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    RISK_OFF = "RISK_OFF"
    PANIC = "PANIC"


@dataclass(frozen=True, slots=True)
class InvestorNetFlow:
    personal: int
    foreign: int
    institution: int
    financial_investment: int
    insurance: int
    investment_trust: int
    private_fund: int
    bank: int
    other_finance: int
    pension_fund_etc: int
    other_corporation: int


@dataclass(frozen=True, slots=True)
class ProgramNetFlow:
    arbitrage: int
    non_arbitrage: int
    total: int


@dataclass(frozen=True, slots=True)
class DailyMarketFlow:
    trading_date: str
    kospi_return_pct: Decimal
    personal: int
    foreign: int
    institution: int
    financial_investment: int
    insurance: int
    investment_trust: int
    private_fund: int
    bank: int
    other_finance: int
    pension_fund_etc: int
    other_corporation: int


@dataclass(frozen=True, slots=True)
class FlowQualityFeatures:
    as_of_date: str
    foreign_positive_streak: int
    pension_positive_streak: int
    investment_trust_positive_streak: int
    joint_positive_days_5: int
    joint_positive_days_10: int
    financial_investment_only_warning: bool
    latest_price_flow_regime: str


@dataclass(frozen=True, slots=True)
class MarketWideSnapshot:
    observed_at: datetime
    kospi_index: Decimal
    kospi_return_pct: Decimal
    kospi_open: Decimal
    kospi_high: Decimal
    kospi_low: Decimal
    accumulated_trading_value_million: int
    rising_issues: int
    flat_issues: int
    declining_issues: int
    upper_limit_issues: int
    lower_limit_issues: int
    investor: InvestorNetFlow
    program: ProgramNetFlow
    foreign_delta_5m: int = 0
    foreign_delta_15m: int = 0
    institution_delta_5m: int = 0
    institution_delta_15m: int = 0
    pension_delta_5m: int = 0
    pension_delta_15m: int = 0
    program_delta_5m: int = 0
    program_delta_15m: int = 0
    flow_quality: FlowQualityFeatures | None = None
    provider_complete: bool = True

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.kospi_index <= 0:
            raise ValueError("kospi_index must be positive")
        for value in (
            self.accumulated_trading_value_million,
            self.rising_issues,
            self.flat_issues,
            self.declining_issues,
            self.upper_limit_issues,
            self.lower_limit_issues,
        ):
            if value < 0:
                raise ValueError("market counts and trading value must not be negative")

    @property
    def listed_issue_count(self) -> int:
        return self.rising_issues + self.flat_issues + self.declining_issues

    @property
    def declining_issue_ratio(self) -> Decimal:
        if self.listed_issue_count == 0:
            return Decimal("0")
        return Decimal(self.declining_issues) / Decimal(self.listed_issue_count)

    @property
    def foreign_net_ratio(self) -> Decimal:
        if self.accumulated_trading_value_million <= 0:
            return Decimal("0")
        return Decimal(self.investor.foreign) / Decimal(
            self.accumulated_trading_value_million
        )

    @property
    def pension_absorption_ratio(self) -> Decimal:
        """Positive proxy when pension funds buy against foreign net selling."""
        if self.investor.foreign >= 0 or self.investor.pension_fund_etc <= 0:
            return Decimal("0")
        return min(
            Decimal("1"),
            Decimal(self.investor.pension_fund_etc)
            / abs(Decimal(self.investor.foreign)),
        )
