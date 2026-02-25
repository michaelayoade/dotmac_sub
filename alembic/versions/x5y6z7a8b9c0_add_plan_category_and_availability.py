"""Add plan category and availability fields to catalog_offers.

Revision ID: x5y6z7a8b9c0
Revises: w4x5y6z7a8b9
Create Date: 2026-02-25 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "x5y6z7a8b9c0"
down_revision: str = "w4x5y6z7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "catalog_offers"


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_columns = {col["name"] for col in inspector.get_columns(TABLE)}

    # Create the plancategory enum type if it does not exist
    plancategory_enum = postgresql.ENUM(
        "internet", "recurring", "one_time", "bundle",
        name="plancategory",
        create_type=False,
    )
    # Check if type already exists before creating
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'plancategory'")
    ).fetchone()
    if not result:
        plancategory_enum.create(conn, checkfirst=True)

    if "plan_category" not in existing_columns:
        op.add_column(
            TABLE,
            sa.Column(
                "plan_category",
                plancategory_enum,
                server_default="internet",
                nullable=False,
            ),
        )

    if "hide_on_admin_portal" not in existing_columns:
        op.add_column(
            TABLE,
            sa.Column(
                "hide_on_admin_portal",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
        )

    if "service_description" not in existing_columns:
        op.add_column(
            TABLE,
            sa.Column("service_description", sa.Text(), nullable=True),
        )

    if "burst_profile" not in existing_columns:
        op.add_column(
            TABLE,
            sa.Column("burst_profile", sa.String(120), nullable=True),
        )

    if "prepaid_period" not in existing_columns:
        op.add_column(
            TABLE,
            sa.Column("prepaid_period", sa.String(40), nullable=True),
        )

    if "allowed_change_plan_ids" not in existing_columns:
        op.add_column(
            TABLE,
            sa.Column("allowed_change_plan_ids", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_columns = {col["name"] for col in inspector.get_columns(TABLE)}

    for col in [
        "allowed_change_plan_ids",
        "prepaid_period",
        "burst_profile",
        "service_description",
        "hide_on_admin_portal",
        "plan_category",
    ]:
        if col in existing_columns:
            op.drop_column(TABLE, col)

    # Drop the enum type if it exists
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'plancategory'")
    ).fetchone()
    if result:
        postgresql.ENUM(name="plancategory").drop(conn, checkfirst=True)
