"""Add optional customer connection type classification."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "649_customer_connection_type"
down_revision = "648_customer_region_spatial_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscribers",
        sa.Column("connection_type", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "ck_subscribers_connection_type",
        "subscribers",
        "connection_type IS NULL OR connection_type IN ('wireless', 'wired')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_subscribers_connection_type", "subscribers", type_="check"
    )
    op.drop_column("subscribers", "connection_type")
