"""Work-order Phase 2 schema parity staging columns.

Revision ID: 222_work_order_phase2_schema_parity
Revises: 221_support_tickets_phase1_expand
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "222_work_order_phase2_schema_parity"
down_revision = "221_support_tickets_phase1_expand"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table_name: str, column_name: str) -> bool:
    return any(c["name"] == column_name for c in _inspector().get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in _inspector().get_indexes(table_name))


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def _drop_column_if_present(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    _add_column_if_missing("work_order_mirror", sa.Column("description", sa.Text()))
    _add_column_if_missing(
        "work_order_mirror", sa.Column("crm_ticket_id", sa.String(length=64))
    )
    _add_column_if_missing(
        "work_order_mirror", sa.Column("crm_project_id", sa.String(length=64))
    )
    _add_column_if_missing(
        "work_order_mirror",
        sa.Column("assigned_to_crm_person_id", sa.String(length=64)),
    )
    _add_column_if_missing(
        "work_order_mirror", sa.Column("assigned_to_name", sa.String(length=160))
    )
    _add_column_if_missing(
        "work_order_mirror", sa.Column("started_at", sa.DateTime(timezone=True))
    )
    _add_column_if_missing(
        "work_order_mirror", sa.Column("paused_at", sa.DateTime(timezone=True))
    )
    _add_column_if_missing(
        "work_order_mirror", sa.Column("resumed_at", sa.DateTime(timezone=True))
    )
    _add_column_if_missing(
        "work_order_mirror", sa.Column("total_active_seconds", sa.Integer())
    )
    _add_column_if_missing("work_order_mirror", sa.Column("required_skills", sa.JSON()))
    _add_column_if_missing("work_order_mirror", sa.Column("tags", sa.JSON()))
    _add_column_if_missing("work_order_mirror", sa.Column("access_notes", sa.Text()))
    _add_column_if_missing(
        "work_order_mirror",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    _add_column_if_missing("work_order_mirror", sa.Column("metadata", sa.JSON()))

    if not _has_index("work_order_mirror", "ix_work_order_mirror_crm_ticket_id"):
        op.create_index(
            "ix_work_order_mirror_crm_ticket_id",
            "work_order_mirror",
            ["crm_ticket_id"],
        )
    if not _has_index("work_order_mirror", "ix_work_order_mirror_crm_project_id"):
        op.create_index(
            "ix_work_order_mirror_crm_project_id",
            "work_order_mirror",
            ["crm_project_id"],
        )
    if not _has_index(
        "work_order_mirror", "ix_work_order_mirror_assigned_to_crm_person_id"
    ):
        op.create_index(
            "ix_work_order_mirror_assigned_to_crm_person_id",
            "work_order_mirror",
            ["assigned_to_crm_person_id"],
        )
    if not _has_index("work_order_mirror", "ix_work_order_mirror_status_schedule"):
        op.create_index(
            "ix_work_order_mirror_status_schedule",
            "work_order_mirror",
            ["status", "scheduled_start"],
            postgresql_where=sa.text("is_active"),
        )


def downgrade() -> None:
    if _has_index("work_order_mirror", "ix_work_order_mirror_status_schedule"):
        op.drop_index(
            "ix_work_order_mirror_status_schedule", table_name="work_order_mirror"
        )
    if _has_index(
        "work_order_mirror", "ix_work_order_mirror_assigned_to_crm_person_id"
    ):
        op.drop_index(
            "ix_work_order_mirror_assigned_to_crm_person_id",
            table_name="work_order_mirror",
        )
    if _has_index("work_order_mirror", "ix_work_order_mirror_crm_project_id"):
        op.drop_index(
            "ix_work_order_mirror_crm_project_id", table_name="work_order_mirror"
        )
    if _has_index("work_order_mirror", "ix_work_order_mirror_crm_ticket_id"):
        op.drop_index(
            "ix_work_order_mirror_crm_ticket_id", table_name="work_order_mirror"
        )

    for column_name in (
        "metadata",
        "is_active",
        "access_notes",
        "tags",
        "required_skills",
        "total_active_seconds",
        "resumed_at",
        "paused_at",
        "started_at",
        "assigned_to_name",
        "assigned_to_crm_person_id",
        "crm_project_id",
        "crm_ticket_id",
        "description",
    ):
        _drop_column_if_present("work_order_mirror", column_name)
