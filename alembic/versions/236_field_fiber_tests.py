"""Add native field fiber test results.

Revision ID: 236_field_fiber_tests
Revises: 236_field_movements
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "236_field_fiber_tests"
down_revision = "236_field_movements"
branch_labels = None
depends_on = None

_TABLE = "field_fiber_test_results"


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
        sa.Column("asset_type", sa.String(length=80), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("test_type", sa.String(length=40), nullable=False),
        sa.Column("wavelength_nm", sa.Integer()),
        sa.Column("value_db", sa.Float()),
        sa.Column("unit", sa.String(length=16)),
        sa.Column("passed", sa.Boolean()),
        sa.Column("instrument", sa.String(length=120)),
        sa.Column("attachment_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "measured_by_technician_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("technician_profiles.id"),
            nullable=False,
        ),
        sa.Column(
            "measured_by_person_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "measured_by_system_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id"),
        ),
        sa.Column("measured_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text()),
        sa.Column("client_ref", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "test_type IN ('otdr', 'optical_power', 'insertion_loss', 'reflectance', 'continuity', 'other')",
            name="ck_field_fiber_tests_test_type",
        ),
    )
    op.create_index(
        "ix_field_fiber_tests_mirror_created",
        _TABLE,
        ["work_order_mirror_id", "created_at"],
    )
    op.create_index(
        "ix_field_fiber_tests_crm_work_order_id", _TABLE, ["crm_work_order_id"]
    )
    op.create_index("ix_field_fiber_tests_asset", _TABLE, ["asset_type", "asset_id"])
    op.create_index(
        "ix_field_fiber_tests_client_ref",
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
    op.drop_index("ix_field_fiber_tests_client_ref", table_name=_TABLE)
    op.drop_index("ix_field_fiber_tests_asset", table_name=_TABLE)
    op.drop_index("ix_field_fiber_tests_crm_work_order_id", table_name=_TABLE)
    op.drop_index("ix_field_fiber_tests_mirror_created", table_name=_TABLE)
    op.drop_table(_TABLE)
