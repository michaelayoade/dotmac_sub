"""Add work_order_mirror + work_order_sync_state (local copy of CRM work orders).

Revision ID: 190_add_work_order_mirror
Revises: 189_add_project_mirror
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "190_add_work_order_mirror"
down_revision = "189_add_project_mirror"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_order_mirror",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("crm_work_order_id", sa.String(length=64), nullable=False),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="scheduled"
        ),
        sa.Column("work_type", sa.String(length=20), nullable=True),
        sa.Column("priority", sa.String(length=20), nullable=True),
        sa.Column("technician_name", sa.String(length=160), nullable=True),
        sa.Column("technician_phone", sa.String(length=40), nullable=True),
        sa.Column("address", sa.String(length=255), nullable=True),
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_arrival_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_duration_minutes", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("work_order_created_at", sa.DateTime(timezone=True), nullable=True),
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
        "uq_work_order_mirror_crm_id", "work_order_mirror", ["crm_work_order_id"]
    )
    op.create_index(
        "ix_work_order_mirror_subscriber_id", "work_order_mirror", ["subscriber_id"]
    )

    op.create_table(
        "work_order_sync_state",
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
    op.drop_table("work_order_sync_state")
    op.drop_index("ix_work_order_mirror_subscriber_id", table_name="work_order_mirror")
    op.drop_constraint(
        "uq_work_order_mirror_crm_id", "work_order_mirror", type_="unique"
    )
    op.drop_table("work_order_mirror")
