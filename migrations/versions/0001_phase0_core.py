"""phase0 core tables

Revision ID: 0001_phase0_core
Revises:
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_phase0_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "buy_approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("max_amount_krw", sa.BigInteger(), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("limit_price", sa.BigInteger(), nullable=True),
        sa.Column("max_acceptable_price", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_holding_days", sa.Integer(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "order_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("approval_id", sa.String(36), nullable=True),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("cause", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "broker_orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("order_intent_id", sa.String(36), nullable=False),
        sa.Column("broker_order_no", sa.String(32), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("raw_response_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "fills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("broker_order_id", sa.String(36), nullable=False),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("price", sa.BigInteger(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "positions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("average_entry_price", sa.BigInteger(), nullable=False),
        sa.Column("hard_stop_price", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("symbol", "generation", name="uq_position_generation"),
    )
    op.create_table(
        "risk_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("position_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("price", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("risk_events")
    op.drop_table("positions")
    op.drop_table("fills")
    op.drop_table("broker_orders")
    op.drop_table("order_intents")
    op.drop_table("buy_approvals")

