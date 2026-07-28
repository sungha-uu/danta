"""execution runtime metadata

Revision ID: 0002_execution_runtime
Revises: 0001_phase0_core
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_execution_runtime"
down_revision: str | None = "0001_phase0_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("order_intents") as batch:
        batch.alter_column(
            "approval_id",
            existing_type=sa.String(36),
            type_=sa.String(64),
            existing_nullable=True,
        )
        batch.add_column(sa.Column("generation", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("priority", sa.Integer(), nullable=False, server_default="50"))
        batch.add_column(
            sa.Column("order_type", sa.String(16), nullable=False, server_default="MARKET")
        )
        batch.add_column(sa.Column("limit_price", sa.BigInteger(), nullable=True))
        batch.add_column(
            sa.Column(
                "policy_version",
                sa.String(64),
                nullable=False,
                server_default="unknown",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("order_intents") as batch:
        batch.drop_column("policy_version")
        batch.drop_column("limit_price")
        batch.drop_column("order_type")
        batch.drop_column("priority")
        batch.drop_column("generation")
        batch.alter_column(
            "approval_id",
            existing_type=sa.String(64),
            type_=sa.String(36),
            existing_nullable=True,
        )
