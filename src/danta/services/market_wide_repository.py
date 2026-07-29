from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from danta.db.models import MarketInvestorDailyModel, MarketWideSnapshotModel
from danta.domain.market_wide import (
    DailyMarketFlow,
    MarketWideRiskLevel,
    MarketWideSnapshot,
)
from danta.services.market_guard import MarketGuardDecision


class MarketWideRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(
        self,
        snapshot: MarketWideSnapshot,
        decision: MarketGuardDecision,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                MarketWideSnapshotModel(
                    id=str(uuid4()),
                    observed_at=snapshot.observed_at,
                    risk_level=decision.level.value,
                    risk_score=decision.stress_score,
                    kospi_index=snapshot.kospi_index,
                    kospi_return_pct=snapshot.kospi_return_pct,
                    accumulated_trading_value_million=(
                        snapshot.accumulated_trading_value_million
                    ),
                    rising_issues=snapshot.rising_issues,
                    flat_issues=snapshot.flat_issues,
                    declining_issues=snapshot.declining_issues,
                    personal_net_million=snapshot.investor.personal,
                    foreign_net_million=snapshot.investor.foreign,
                    institution_net_million=snapshot.investor.institution,
                    pension_net_million=snapshot.investor.pension_fund_etc,
                    program_net_million=snapshot.program.total,
                    provider_complete=snapshot.provider_complete,
                    payload=market_status_payload(snapshot, decision),
                )
            )
            await session.commit()

    async def recent_payloads(
        self,
        *,
        minutes: int = 20,
    ) -> list[dict[str, object]]:
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(MarketWideSnapshotModel)
                    .where(MarketWideSnapshotModel.observed_at >= cutoff)
                    .order_by(MarketWideSnapshotModel.observed_at)
                )
            ).all()
            return [row.payload for row in rows]

    async def save_daily_flows(self, flows: list[DailyMarketFlow]) -> None:
        collected_at = datetime.now(UTC)
        async with self._session_factory() as session:
            for flow in flows:
                row = await session.get(MarketInvestorDailyModel, flow.trading_date)
                values = {
                    "kospi_return_pct": flow.kospi_return_pct,
                    "personal_net_million": flow.personal,
                    "foreign_net_million": flow.foreign,
                    "institution_net_million": flow.institution,
                    "financial_investment_net_million": flow.financial_investment,
                    "insurance_net_million": flow.insurance,
                    "investment_trust_net_million": flow.investment_trust,
                    "private_fund_net_million": flow.private_fund,
                    "bank_net_million": flow.bank,
                    "other_finance_net_million": flow.other_finance,
                    "pension_net_million": flow.pension_fund_etc,
                    "other_corporation_net_million": flow.other_corporation,
                    "collected_at": collected_at,
                }
                if row is None:
                    session.add(
                        MarketInvestorDailyModel(
                            trading_date=flow.trading_date,
                            **values,
                        )
                    )
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
            await session.commit()


def market_status_payload(
    snapshot: MarketWideSnapshot,
    decision: MarketGuardDecision,
) -> dict[str, object]:
    return {
        "schema_version": "danta-market-status-v1",
        "observed_at": snapshot.observed_at.isoformat(),
        "risk": {
            "level": decision.level.value,
            "entry_guard": decision.risk.value,
            "stress_score": str(decision.stress_score),
            "reason_codes": list(decision.reason_codes),
        },
        "kospi": {
            "index": str(snapshot.kospi_index),
            "return_pct": str(snapshot.kospi_return_pct),
            "open": str(snapshot.kospi_open),
            "high": str(snapshot.kospi_high),
            "low": str(snapshot.kospi_low),
            "accumulated_trading_value_million": (
                snapshot.accumulated_trading_value_million
            ),
        },
        "breadth": {
            "rising": snapshot.rising_issues,
            "flat": snapshot.flat_issues,
            "declining": snapshot.declining_issues,
            "declining_ratio": str(snapshot.declining_issue_ratio),
            "upper_limit": snapshot.upper_limit_issues,
            "lower_limit": snapshot.lower_limit_issues,
        },
        "investor_net_million": {
            "personal": snapshot.investor.personal,
            "foreign": snapshot.investor.foreign,
            "institution": snapshot.investor.institution,
            "financial_investment": snapshot.investor.financial_investment,
            "insurance": snapshot.investor.insurance,
            "investment_trust": snapshot.investor.investment_trust,
            "private_fund": snapshot.investor.private_fund,
            "bank": snapshot.investor.bank,
            "other_finance": snapshot.investor.other_finance,
            "pension_fund_etc": snapshot.investor.pension_fund_etc,
            "other_corporation": snapshot.investor.other_corporation,
        },
        "program_net_million": {
            "arbitrage": snapshot.program.arbitrage,
            "non_arbitrage": snapshot.program.non_arbitrage,
            "total": snapshot.program.total,
        },
        "deltas_million": {
            "foreign_5m": snapshot.foreign_delta_5m,
            "foreign_15m": snapshot.foreign_delta_15m,
            "institution_5m": snapshot.institution_delta_5m,
            "institution_15m": snapshot.institution_delta_15m,
            "pension_5m": snapshot.pension_delta_5m,
            "pension_15m": snapshot.pension_delta_15m,
            "program_5m": snapshot.program_delta_5m,
            "program_15m": snapshot.program_delta_15m,
        },
        "derived": {
            "foreign_net_ratio": str(snapshot.foreign_net_ratio),
            "pension_absorption_ratio": str(snapshot.pension_absorption_ratio),
        },
        "flow_quality": (
            None
            if snapshot.flow_quality is None
            else {
                "as_of_date": snapshot.flow_quality.as_of_date,
                "foreign_positive_streak": (
                    snapshot.flow_quality.foreign_positive_streak
                ),
                "pension_positive_streak": (
                    snapshot.flow_quality.pension_positive_streak
                ),
                "investment_trust_positive_streak": (
                    snapshot.flow_quality.investment_trust_positive_streak
                ),
                "joint_positive_days_5": snapshot.flow_quality.joint_positive_days_5,
                "joint_positive_days_10": (
                    snapshot.flow_quality.joint_positive_days_10
                ),
                "financial_investment_only_warning": (
                    snapshot.flow_quality.financial_investment_only_warning
                ),
                "latest_price_flow_regime": (
                    snapshot.flow_quality.latest_price_flow_regime
                ),
            }
        ),
        "provider_complete": snapshot.provider_complete,
    }


def risk_level_from_payload(payload: dict[str, object]) -> MarketWideRiskLevel | None:
    risk = payload.get("risk")
    if not isinstance(risk, dict):
        return None
    try:
        return MarketWideRiskLevel(str(risk.get("level")))
    except ValueError:
        return None


def decimal_from_payload(
    payload: dict[str, object],
    *path: str,
) -> Decimal | None:
    value: object = payload
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    try:
        return Decimal(str(value))
    except (ValueError, TypeError):
        return None
