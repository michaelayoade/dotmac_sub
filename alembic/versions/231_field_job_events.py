"""Add native field job transition events.

Revision ID: 231_field_job_events
Revises: 230_field_attachments
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "231_field_job_events"
down_revision = "230_field_attachments"
branch_labels = None
depends_on = None

_TABLE = "field_job_events"


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "work_order_mirror_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_order_mirror.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("crm_work_order_id", sa.String(length=64), nullable=False),
        sa.Column(
            "author_technician_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("technician_profiles.id"),
            nullable=False,
        ),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "system_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id"),
        ),
        sa.Column("event", sa.String(length=40), nullable=False),
        sa.Column("previous_status", sa.String(length=40)),
        sa.Column("new_status", sa.String(length=40)),
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column("note", sa.Text()),
        sa.Column("payload", sa.JSON()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "event IN ('accept', 'en_route', 'arrived', 'start', 'pause', 'hold', "
            "'resume', 'complete', 'unable_to_complete')",
            name="ck_field_job_events_event",
        ),
    )
    op.create_index(
        "ix_field_job_events_mirror_occurred",
        _TABLE,
        ["work_order_mirror_id", "occurred_at"],
    )
    op.create_index(
        "ix_field_job_events_crm_work_order_id", _TABLE, ["crm_work_order_id"]
    )
    op.create_index(
        "ix_field_job_events_author_occurred",
        _TABLE,
        ["author_technician_id", "occurred_at"],
    )
    op.create_index(
        "ix_field_job_events_client_event_id",
        _TABLE,
        ["client_event_id"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table(_TABLE):
        return
    op.drop_index("ix_field_job_events_client_event_id", table_name=_TABLE)
    op.drop_index("ix_field_job_events_author_occurred", table_name=_TABLE)
    op.drop_index("ix_field_job_events_crm_work_order_id", table_name=_TABLE)
    op.drop_index("ix_field_job_events_mirror_occurred", table_name=_TABLE)
    op.drop_table(_TABLE)
