"""market-wide risk monitoring snapshots

Revision ID: 0003_market_wide_monitor
Revises: 0002_execution_runtime
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_market_wide_monitor"
down_revision: str | None = "0002_execution_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_wide_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("risk_score", sa.Numeric(8, 6), nullable=False),
        sa.Column("kospi_index", sa.Numeric(12, 4), nullable=False),
        sa.Column("kospi_return_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column(
            "accumulated_trading_value_million", sa.BigInteger(), nullable=False
        ),
        sa.Column("rising_issues", sa.Integer(), nullable=False),
        sa.Column("flat_issues", sa.Integer(), nullable=False),
        sa.Column("declining_issues", sa.Integer(), nullable=False),
        sa.Column("personal_net_million", sa.BigInteger(), nullable=False),
        sa.Column("foreign_net_million", sa.BigInteger(), nullable=False),
        sa.Column("institution_net_million", sa.BigInteger(), nullable=False),
        sa.Column("pension_net_million", sa.BigInteger(), nullable=False),
        sa.Column("program_net_million", sa.BigInteger(), nullable=False),
        sa.Column("provider_complete", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "observed_at", name="uq_market_wide_snapshots_observed_at"
        ),
    )
    op.create_index(
        "ix_market_wide_snapshots_observed_at",
        "market_wide_snapshots",
        ["observed_at"],
    )
    op.create_index(
        "ix_market_wide_snapshots_risk_level",
        "market_wide_snapshots",
        ["risk_level"],
    )
    op.create_table(
        "market_investor_daily",
        sa.Column("trading_date", sa.String(8), primary_key=True),
        sa.Column("kospi_return_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("personal_net_million", sa.BigInteger(), nullable=False),
        sa.Column("foreign_net_million", sa.BigInteger(), nullable=False),
        sa.Column("institution_net_million", sa.BigInteger(), nullable=False),
        sa.Column(
            "financial_investment_net_million", sa.BigInteger(), nullable=False
        ),
        sa.Column("insurance_net_million", sa.BigInteger(), nullable=False),
        sa.Column("investment_trust_net_million", sa.BigInteger(), nullable=False),
        sa.Column("private_fund_net_million", sa.BigInteger(), nullable=False),
        sa.Column("bank_net_million", sa.BigInteger(), nullable=False),
        sa.Column("other_finance_net_million", sa.BigInteger(), nullable=False),
        sa.Column("pension_net_million", sa.BigInteger(), nullable=False),
        sa.Column("other_corporation_net_million", sa.BigInteger(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("market_investor_daily")
    op.drop_index(
        "ix_market_wide_snapshots_risk_level",
        table_name="market_wide_snapshots",
    )
    op.drop_index(
        "ix_market_wide_snapshots_observed_at",
        table_name="market_wide_snapshots",
    )
    op.drop_table("market_wide_snapshots")
