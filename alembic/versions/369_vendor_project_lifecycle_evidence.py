"""Add durable vendor project lifecycle transition evidence.

Revision ID: 369_vendor_lifecycle_evidence
Revises: 368_merge_legacy_ip_assignments_branch
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "369_vendor_lifecycle_evidence"
down_revision = "368_merge_legacy_ip_assignments_branch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "installation_project_lifecycle_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("from_status", sa.String(length=40), nullable=False),
        sa.Column("to_status", sa.String(length=40), nullable=False),
        sa.Column("actor_type", sa.String(length=40), nullable=False),
        sa.Column("actor_id", sa.String(length=120), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "from_status <> to_status",
            name="ck_installation_project_lifecycle_status_change",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["installation_projects.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_installation_project_lifecycle_event_id",
        "installation_project_lifecycle_events",
        ["event_id"],
        unique=True,
    )
    op.create_index(
        "ix_installation_project_lifecycle_event_type",
        "installation_project_lifecycle_events",
        ["event_type"],
    )
    op.create_index(
        "ix_installation_project_lifecycle_actor_id",
        "installation_project_lifecycle_events",
        ["actor_id"],
    )
    op.create_index(
        "ix_installation_project_lifecycle_vendor_id",
        "installation_project_lifecycle_events",
        ["vendor_id"],
    )
    op.create_index(
        "ix_installation_project_lifecycle_project_occurred",
        "installation_project_lifecycle_events",
        ["project_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_installation_project_lifecycle_project_occurred",
        table_name="installation_project_lifecycle_events",
    )
    op.drop_index(
        "ix_installation_project_lifecycle_vendor_id",
        table_name="installation_project_lifecycle_events",
    )
    op.drop_index(
        "ix_installation_project_lifecycle_actor_id",
        table_name="installation_project_lifecycle_events",
    )
    op.drop_index(
        "ix_installation_project_lifecycle_event_type",
        table_name="installation_project_lifecycle_events",
    )
    op.drop_index(
        "ix_installation_project_lifecycle_event_id",
        table_name="installation_project_lifecycle_events",
    )
    op.drop_table("installation_project_lifecycle_events")
