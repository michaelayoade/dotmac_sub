"""Add captive_redirect_enabled to subscribers.

Per-subscriber flag to control whether blocked/expired subscribers
get HTTP-redirected to the customer portal for self-service renewal.

Revision ID: g0a1b2c3d4e5
Revises: f9a0b1c2d3e4
Create Date: 2026-03-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g0a1b2c3d4e5"
down_revision: str = "f9a0b1c2d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("subscribers")]
    if "captive_redirect_enabled" not in columns:
        op.add_column(
            "subscribers",
            sa.Column(
                "captive_redirect_enabled",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    op.drop_column("subscribers", "captive_redirect_enabled")
