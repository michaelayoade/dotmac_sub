"""Add quote_mirror + quote_sync_state (local copy of CRM self-serve quotes).

Revision ID: 191_add_quote_mirror
Revises: 190_add_work_order_mirror
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "191_add_quote_mirror"
down_revision = "190_add_work_order_mirror"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quote_mirror",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("crm_quote_id", sa.String(length=64), nullable=False),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="draft"
        ),
        sa.Column(
            "currency", sa.String(length=3), nullable=False, server_default="NGN"
        ),
        sa.Column("total", sa.String(length=32), nullable=False, server_default="0"),
        sa.Column(
            "deposit_amount", sa.String(length=32), nullable=False, server_default="0"
        ),
        sa.Column("deposit_percent", sa.Integer(), nullable=True),
        sa.Column(
            "deposit_paid", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("feasibility_coverage", sa.String(length=20), nullable=True),
        sa.Column(
            "estimate_provisional",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("address", sa.String(length=255), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("sales_order_id", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("quote_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint(
        "uq_quote_mirror_crm_id", "quote_mirror", ["crm_quote_id"]
    )
    op.create_index("ix_quote_mirror_subscriber_id", "quote_mirror", ["subscriber_id"])

    op.create_table(
        "quote_sync_state",
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("quote_sync_state")
    op.drop_index("ix_quote_mirror_subscriber_id", table_name="quote_mirror")
    op.drop_constraint("uq_quote_mirror_crm_id", "quote_mirror", type_="unique")
    op.drop_table("quote_mirror")
