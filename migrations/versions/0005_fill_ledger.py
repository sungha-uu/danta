"""persist cumulative fill watermarks

Revision ID: 0005_fill_ledger
Revises: 0004_runtime_recovery
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_fill_ledger"
down_revision: str | None = "0004_runtime_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("fills") as batch:
        batch.add_column(
            sa.Column(
                "cumulative_quantity",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.create_unique_constraint(
            "uq_fill_broker_cumulative_quantity",
            ["broker_order_id", "cumulative_quantity"],
        )


def downgrade() -> None:
    with op.batch_alter_table("fills") as batch:
        batch.drop_constraint(
            "uq_fill_broker_cumulative_quantity", type_="unique"
        )
        batch.drop_column("cumulative_quantity")
