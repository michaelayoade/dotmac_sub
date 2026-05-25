"""Add ticket automation rules table.

Revision ID: 107_add_ticket_automation_rules
Revises: 106_add_subscriber_owned_notifications
Create Date: 2026-05-25
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "107_add_ticket_automation_rules"
down_revision = "106_add_subscriber_owned_notifications"
branch_labels = None
depends_on = None


TRIGGER_NAME = "ticket_automation_trigger"
TRIGGER_VALUES = ("ticket_created", "status_changed", "priority_changed")
ACTION_TYPE_NAME = "ticket_automation_action_type"
ACTION_TYPE_VALUES = (
    "assign_team",
    "assign_technician",
    "set_priority",
    "set_status",
    "set_due_in_hours",
    "add_tag",
)
TABLE_NAME = "support_ticket_automation_rules"


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_type(bind, type_name: str) -> bool:
    return (
        bind.execute(
            sa.text("SELECT 1 FROM pg_type WHERE typname = :n"), {"n": type_name}
        ).scalar_one_or_none()
        is not None
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        if not _has_type(bind, TRIGGER_NAME):
            postgresql.ENUM(*TRIGGER_VALUES, name=TRIGGER_NAME).create(bind)
        if not _has_type(bind, ACTION_TYPE_NAME):
            postgresql.ENUM(*ACTION_TYPE_VALUES, name=ACTION_TYPE_NAME).create(bind)

    if _has_table(bind, TABLE_NAME):
        return

    op.create_table(
        TABLE_NAME,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True) if is_pg else sa.String(length=36),
            primary_key=True,
        ),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "trigger",
            postgresql.ENUM(*TRIGGER_VALUES, name=TRIGGER_NAME, create_type=False)
            if is_pg
            else sa.String(length=40),
            nullable=False,
        ),
        sa.Column(
            "conditions",
            postgresql.JSONB() if is_pg else sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if is_pg else sa.text("'{}'"),
        ),
        sa.Column(
            "action_type",
            postgresql.ENUM(
                *ACTION_TYPE_VALUES, name=ACTION_TYPE_NAME, create_type=False
            )
            if is_pg
            else sa.String(length=40),
            nullable=False,
        ),
        sa.Column(
            "action_value",
            postgresql.JSONB() if is_pg else sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if is_pg else sa.text("'{}'"),
        ),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False, server_default=sa.text("100")
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_support_automation_trigger_active",
        TABLE_NAME,
        ["trigger", "is_active"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if _has_table(bind, TABLE_NAME):
        op.drop_index("ix_support_automation_trigger_active", table_name=TABLE_NAME)
        op.drop_table(TABLE_NAME)

    if is_pg:
        if _has_type(bind, ACTION_TYPE_NAME):
            op.execute(f"DROP TYPE {ACTION_TYPE_NAME}")
        if _has_type(bind, TRIGGER_NAME):
            op.execute(f"DROP TYPE {TRIGGER_NAME}")
