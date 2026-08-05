from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from danta.db.base import Base


class BuyApprovalModel(Base):
    __tablename__ = "buy_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    max_amount_krw: Mapped[int] = mapped_column(BigInteger)
    order_type: Mapped[str] = mapped_column(String(16))
    limit_price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_acceptable_price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_holding_days: Mapped[int] = mapped_column(Integer)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderIntentModel(Base):
    __tablename__ = "order_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    side: Mapped[str] = mapped_column(String(8))
    cause: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=50)
    order_type: Mapped[str] = mapped_column(String(16), default="MARKET")
    limit_price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    policy_version: Mapped[str] = mapped_column(String(64), default="unknown")
    status: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BrokerOrderModel(Base):
    __tablename__ = "broker_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_intent_id: Mapped[str] = mapped_column(String(36), index=True)
    broker_order_no: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24))
    raw_response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FillModel(Base):
    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint(
            "broker_order_id",
            "cumulative_quantity",
            name="uq_fill_broker_cumulative_quantity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    broker_order_id: Mapped[str] = mapped_column(String(36), index=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    price: Mapped[int] = mapped_column(BigInteger)
    quantity: Mapped[int] = mapped_column(Integer)
    cumulative_quantity: Mapped[int] = mapped_column(Integer)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PositionModel(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("symbol", "generation", name="uq_position_generation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    average_entry_price: Mapped[int] = mapped_column(BigInteger)
    hard_stop_price: Mapped[int] = mapped_column(BigInteger)
    peak_return_pct: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0")
    )
    status: Mapped[str] = mapped_column(String(24))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RiskEventModel(Base):
    __tablename__ = "risk_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    position_id: Mapped[str] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    price: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLogModel(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketWideSnapshotModel(Base):
    __tablename__ = "market_wide_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), unique=True, index=True
    )
    risk_level: Mapped[str] = mapped_column(String(16), index=True)
    risk_score: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    kospi_index: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    kospi_return_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    accumulated_trading_value_million: Mapped[int] = mapped_column(BigInteger)
    rising_issues: Mapped[int] = mapped_column(Integer)
    flat_issues: Mapped[int] = mapped_column(Integer)
    declining_issues: Mapped[int] = mapped_column(Integer)
    personal_net_million: Mapped[int] = mapped_column(BigInteger)
    foreign_net_million: Mapped[int] = mapped_column(BigInteger)
    institution_net_million: Mapped[int] = mapped_column(BigInteger)
    pension_net_million: Mapped[int] = mapped_column(BigInteger)
    program_net_million: Mapped[int] = mapped_column(BigInteger)
    provider_complete: Mapped[bool] = mapped_column(Boolean)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class MarketInvestorDailyModel(Base):
    __tablename__ = "market_investor_daily"

    trading_date: Mapped[str] = mapped_column(String(8), primary_key=True)
    kospi_return_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    personal_net_million: Mapped[int] = mapped_column(BigInteger)
    foreign_net_million: Mapped[int] = mapped_column(BigInteger)
    institution_net_million: Mapped[int] = mapped_column(BigInteger)
    financial_investment_net_million: Mapped[int] = mapped_column(BigInteger)
    insurance_net_million: Mapped[int] = mapped_column(BigInteger)
    investment_trust_net_million: Mapped[int] = mapped_column(BigInteger)
    private_fund_net_million: Mapped[int] = mapped_column(BigInteger)
    bank_net_million: Mapped[int] = mapped_column(BigInteger)
    other_finance_net_million: Mapped[int] = mapped_column(BigInteger)
    pension_net_million: Mapped[int] = mapped_column(BigInteger)
    other_corporation_net_million: Mapped[int] = mapped_column(BigInteger)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
