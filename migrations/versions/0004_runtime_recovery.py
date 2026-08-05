"""durable runtime recovery state

Revision ID: 0004_runtime_recovery
Revises: 0003_market_wide_monitor
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_runtime_recovery"
down_revision: str | None = "0003_market_wide_monitor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column(
            "peak_return_pct",
            sa.Numeric(10, 6),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("positions", "peak_return_pct")
