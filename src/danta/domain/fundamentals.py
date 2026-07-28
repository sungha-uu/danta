from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

FUNDAMENTAL_CALCULATION_VERSION: Final = "fundamental-snapshot-v1"


class FundamentalSnapshot(BaseModel):
    """Provider-neutral, point-in-time financial context for one stock."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(pattern=r"^[0-9A-Z]{6}$")
    name: str = Field(min_length=1, max_length=80)
    corp_code: str = Field(pattern=r"^\d{8}$")
    as_of_date: date
    fetched_at: datetime
    business_year: int = Field(ge=2015)
    report_code: Literal["11011", "11012", "11013", "11014"]
    report_name: str
    receipt_no: str | None = Field(default=None, pattern=r"^\d{14}$")
    statement_type: Literal["CFS", "OFS"]
    currency: str = Field(default="KRW", min_length=1, max_length=12)
    total_assets: Decimal | None = None
    current_assets: Decimal | None = None
    total_liabilities: Decimal | None = None
    current_liabilities: Decimal | None = None
    total_equity: Decimal | None = None
    revenue: Decimal | None = None
    operating_income: Decimal | None = None
    net_income: Decimal | None = None
    debt_ratio_pct: Decimal | None = None
    current_ratio_pct: Decimal | None = None
    operating_margin_pct: Decimal | None = None
    net_margin_pct: Decimal | None = None
    risk_flags: tuple[str, ...] = ()
    health_status: Literal["HEALTHY", "CAUTION", "RISK", "UNAVAILABLE"]
    source: Literal["OPEN_DART"] = "OPEN_DART"
    calculation_version: Literal["fundamental-snapshot-v1"] = (
        FUNDAMENTAL_CALCULATION_VERSION
    )


class FundamentalSnapshotBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    target_business_year: int = Field(ge=2015)
    target_report_code: Literal["11011", "11012", "11013", "11014"]
    requested_symbols: tuple[str, ...]
    snapshots: tuple[FundamentalSnapshot, ...]
    unavailable_symbols: tuple[str, ...] = ()
    provider_errors: tuple[str, ...] = ()
    source: Literal["OPEN_DART"] = "OPEN_DART"
    calculation_version: Literal["fundamental-snapshot-v1"] = (
        FUNDAMENTAL_CALCULATION_VERSION
    )

    def by_symbol(self) -> dict[str, FundamentalSnapshot]:
        return {item.symbol: item for item in self.snapshots}


def safe_ratio(
    numerator: Decimal | None,
    denominator: Decimal | None,
) -> Decimal | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return (numerator / denominator * Decimal("100")).quantize(Decimal("0.01"))


def financial_risk_flags(
    *,
    total_assets: Decimal | None,
    total_liabilities: Decimal | None,
    total_equity: Decimal | None,
    revenue: Decimal | None,
    operating_income: Decimal | None,
    net_income: Decimal | None,
    debt_ratio_pct: Decimal | None,
    current_ratio_pct: Decimal | None,
) -> tuple[str, ...]:
    values = (
        total_assets,
        total_liabilities,
        total_equity,
        revenue,
        operating_income,
        net_income,
    )
    flags: list[str] = []
    if any(value is None for value in values):
        flags.append("REQUIRED_ACCOUNTS_MISSING")
    if total_equity is not None and total_equity <= 0:
        flags.append("NEGATIVE_EQUITY")
    if operating_income is not None and operating_income < 0:
        flags.append("OPERATING_LOSS")
    if net_income is not None and net_income < 0:
        flags.append("NET_LOSS")
    if debt_ratio_pct is not None and debt_ratio_pct >= Decimal("300"):
        flags.append("HIGH_DEBT_RATIO")
    if current_ratio_pct is not None and current_ratio_pct < Decimal("70"):
        flags.append("LOW_CURRENT_RATIO")
    return tuple(flags)


def health_status_for(
    flags: tuple[str, ...],
) -> Literal["HEALTHY", "CAUTION", "RISK", "UNAVAILABLE"]:
    if "REQUIRED_ACCOUNTS_MISSING" in flags:
        return "UNAVAILABLE"
    if "NEGATIVE_EQUITY" in flags:
        return "RISK"
    if flags:
        return "CAUTION"
    return "HEALTHY"
