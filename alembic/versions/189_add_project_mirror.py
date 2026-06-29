"""Add project_mirror + project_sync_state (local copy of CRM projects).

Revision ID: 189_add_project_mirror
Revises: 188_merge_referral_techsupport
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "189_add_project_mirror"
down_revision = "188_merge_referral_techsupport"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_mirror",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("crm_project_id", sa.String(length=64), nullable=False),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="open"
        ),
        sa.Column("project_type", sa.String(length=60), nullable=True),
        sa.Column("progress_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_stage", sa.String(length=120), nullable=True),
        sa.Column("stages", JSONB(), nullable=True),
        sa.Column("customer_address", sa.String(length=255), nullable=True),
        sa.Column("region", sa.String(length=80), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("project_created_at", sa.DateTime(timezone=True), nullable=True),
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
        "uq_project_mirror_crm_project_id", "project_mirror", ["crm_project_id"]
    )
    op.create_index(
        "ix_project_mirror_subscriber_id", "project_mirror", ["subscriber_id"]
    )

    op.create_table(
        "project_sync_state",
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
    op.drop_table("project_sync_state")
    op.drop_index("ix_project_mirror_subscriber_id", table_name="project_mirror")
    op.drop_constraint(
        "uq_project_mirror_crm_project_id", "project_mirror", type_="unique"
    )
    op.drop_table("project_mirror")
