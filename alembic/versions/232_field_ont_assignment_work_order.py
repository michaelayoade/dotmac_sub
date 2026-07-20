"""Link ONT assignments to field work-order mirrors.

Revision ID: 232_field_ont_assignment_work_order
Revises: 231_field_job_events
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "232_field_ont_assignment_work_order"
down_revision = "231_field_job_events"
branch_labels = None
depends_on = None

_TABLE = "ont_assignments"
_COLUMN = "work_order_mirror_id"
_INDEX = "ix_ont_assignments_work_order_mirror_id"
_FK = "fk_ont_assignments_work_order_mirror_id"


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def _foreign_keys(table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in inspect(op.get_bind()).get_foreign_keys(table_name)
    }


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _COLUMN not in _columns(_TABLE):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, postgresql.UUID(as_uuid=True), nullable=True),
        )
    if _FK not in _foreign_keys(_TABLE):
        op.create_foreign_key(
            _FK,
            _TABLE,
            "work_order_mirror",
            [_COLUMN],
            ["id"],
            ondelete="SET NULL",
        )
    indexes = {index["name"] for index in inspect(bind).get_indexes(_TABLE)}
    if _INDEX not in indexes:
        op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    indexes = {index["name"] for index in inspect(bind).get_indexes(_TABLE)}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name=_TABLE)
    if _COLUMN in _columns(_TABLE):
        if _FK in _foreign_keys(_TABLE):
            op.drop_constraint(_FK, _TABLE, type_="foreignkey")
        op.drop_column(_TABLE, _COLUMN)
