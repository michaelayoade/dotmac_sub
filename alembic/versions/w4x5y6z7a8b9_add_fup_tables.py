"""Add fair usage policy tables.

Revision ID: w4x5y6z7a8b9
Revises: v3w4x5y6z7a8
Create Date: 2026-02-25 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "w4x5y6z7a8b9"
down_revision: str | None = "v3w4x5y6z7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # --- Create enum types (idempotent) ---
    for enum_name, values in [
        ("fupconsumptionperiod", ["monthly", "daily", "weekly"]),
        ("fupdirection", ["up", "down", "up_down"]),
        ("fupaction", ["reduce_speed", "block", "notify"]),
        ("fupdataunit", ["mb", "gb", "tb"]),
    ]:
        enum = postgresql.ENUM(*values, name=enum_name, create_type=False)
        enum.create(conn, checkfirst=True)

    # --- fup_policies table ---
    if not inspector.has_table("fup_policies"):
        op.create_table(
            "fup_policies",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
            ),
            sa.Column(
                "offer_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("catalog_offers.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("traffic_accounting_start", sa.Time, nullable=True),
            sa.Column("traffic_accounting_end", sa.Time, nullable=True),
            sa.Column(
                "traffic_inverse_interval",
                sa.Boolean,
                server_default=sa.text("false"),
            ),
            sa.Column("online_accounting_start", sa.Time, nullable=True),
            sa.Column("online_accounting_end", sa.Time, nullable=True),
            sa.Column(
                "online_inverse_interval",
                sa.Boolean,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "traffic_days_of_week",
                postgresql.ARRAY(sa.Integer),
                nullable=True,
            ),
            sa.Column(
                "online_days_of_week",
                postgresql.ARRAY(sa.Integer),
                nullable=True,
            ),
            sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
            ),
        )

    # --- fup_rules table ---
    if not inspector.has_table("fup_rules"):
        op.create_table(
            "fup_rules",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
            ),
            sa.Column(
                "policy_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("fup_policies.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("sort_order", sa.Integer, server_default="0"),
            sa.Column(
                "consumption_period",
                postgresql.ENUM(
                    "monthly",
                    "daily",
                    "weekly",
                    name="fupconsumptionperiod",
                    create_type=False,
                ),
                nullable=False,
                server_default="monthly",
            ),
            sa.Column(
                "direction",
                postgresql.ENUM(
                    "up",
                    "down",
                    "up_down",
                    name="fupdirection",
                    create_type=False,
                ),
                nullable=False,
                server_default="up_down",
            ),
            sa.Column("threshold_amount", sa.Float, nullable=False),
            sa.Column(
                "threshold_unit",
                postgresql.ENUM(
                    "mb",
                    "gb",
                    "tb",
                    name="fupdataunit",
                    create_type=False,
                ),
                nullable=False,
                server_default="gb",
            ),
            sa.Column(
                "action",
                postgresql.ENUM(
                    "reduce_speed",
                    "block",
                    "notify",
                    name="fupaction",
                    create_type=False,
                ),
                nullable=False,
                server_default="reduce_speed",
            ),
            sa.Column("speed_reduction_percent", sa.Float, nullable=True),
            sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
            ),
        )


def downgrade() -> None:
    op.drop_table("fup_rules")
    op.drop_table("fup_policies")

    for enum_name in [
        "fupdataunit",
        "fupaction",
        "fupdirection",
        "fupconsumptionperiod",
    ]:
        postgresql.ENUM(name=enum_name).drop(op.get_bind(), checkfirst=True)
