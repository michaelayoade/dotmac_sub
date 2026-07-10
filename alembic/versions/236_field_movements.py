"""Add native field movement sessions.

Revision ID: 236_field_movements
Revises: 235_field_expense_requests
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "236_field_movements"
down_revision = "235_field_expense_requests"
branch_labels = None
depends_on = None

_TABLE = "field_work_order_movements"


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
            "actor_technician_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("technician_profiles.id"),
            nullable=False,
        ),
        sa.Column("actor_person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "actor_system_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id"),
        ),
        sa.Column("destination_type", sa.String(length=40), nullable=False),
        sa.Column("destination_id", sa.String(length=120)),
        sa.Column("destination_label", sa.String(length=255)),
        sa.Column("destination_latitude", sa.Float()),
        sa.Column("destination_longitude", sa.Float()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("arrived_at", sa.DateTime(timezone=True)),
        sa.Column("start_latitude", sa.Float()),
        sa.Column("start_longitude", sa.Float()),
        sa.Column("arrival_latitude", sa.Float()),
        sa.Column("arrival_longitude", sa.Float()),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("client_ref", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('en_route', 'arrived', 'canceled')",
            name="ck_field_work_order_movements_status",
        ),
    )
    op.create_index(
        "ix_field_work_order_movements_mirror_started",
        _TABLE,
        ["work_order_mirror_id", "started_at"],
    )
    op.create_index(
        "ix_field_work_order_movements_crm_work_order_id",
        _TABLE,
        ["crm_work_order_id"],
    )
    op.create_index(
        "ix_field_work_order_movements_actor_started",
        _TABLE,
        ["actor_technician_id", "started_at"],
    )
    op.create_index(
        "ix_field_work_order_movements_client_ref",
        _TABLE,
        ["client_ref"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table(_TABLE):
        return
    op.drop_index("ix_field_work_order_movements_client_ref", table_name=_TABLE)
    op.drop_index("ix_field_work_order_movements_actor_started", table_name=_TABLE)
    op.drop_index("ix_field_work_order_movements_crm_work_order_id", table_name=_TABLE)
    op.drop_index("ix_field_work_order_movements_mirror_started", table_name=_TABLE)
    op.drop_table(_TABLE)
