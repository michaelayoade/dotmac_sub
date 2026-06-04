"""Add pending ONT provisioning statuses.

Revision ID: 105_add_pending_ont_provisioning_statuses
Revises: 104_add_topup_intents
Create Date: 2026-05-25
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "105_add_pending_ont_provisioning_statuses"
down_revision = "104_add_topup_intents"
branch_labels = None
depends_on = None


_LEGACY_TYPE_NAME = "ontprovisioningstatus"
_TYPE_NAME = "ontprovisioningstatus_v2"
_TABLE_NAME = "ont_units"
_COLUMN_NAME = "provisioning_status"
_PENDING_STATUSES = (
    "pending_acs_registration",
    "pending_service_config",
)
_DOWNGRADE_TYPE_VALUES = (
    "unprovisioned",
    "partial",
    "provisioned",
    "drift_detected",
    "failed",
)
_ALL_TYPE_VALUES = _DOWNGRADE_TYPE_VALUES + _PENDING_STATUSES


def _type_exists(conn, type_name: str) -> bool:
    return (
        conn.execute(
            sa.text("SELECT 1 FROM pg_type WHERE typname = :type_name"),
            {"type_name": type_name},
        ).scalar_one_or_none()
        is not None
    )


def _column_type_name(conn, table_name: str, column_name: str) -> str | None:
    return conn.execute(
        sa.text(
            """
            SELECT c.udt_name
            FROM information_schema.columns AS c
            WHERE c.table_schema = current_schema()
              AND c.table_name = :table_name
              AND c.column_name = :column_name
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    ).scalar_one_or_none()


def _create_enum_type(type_name: str, values: tuple[str, ...]) -> None:
    bind = op.get_bind()
    if _type_exists(bind, type_name):
        return

    values_sql = ", ".join(repr(value) for value in values)
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({values_sql})")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    _create_enum_type(_TYPE_NAME, _ALL_TYPE_VALUES)

    if _column_type_name(bind, _TABLE_NAME, _COLUMN_NAME) != _TYPE_NAME:
        op.alter_column(
            _TABLE_NAME,
            _COLUMN_NAME,
            existing_type=postgresql.ENUM(name=_LEGACY_TYPE_NAME, create_type=False),
            type_=postgresql.ENUM(*_ALL_TYPE_VALUES, name=_TYPE_NAME, create_type=False),
            postgresql_using=f"{_COLUMN_NAME}::text::{_TYPE_NAME}",
            existing_nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        f"UPDATE {_TABLE_NAME} "
        f"SET {_COLUMN_NAME} = 'partial' "
        f"WHERE {_COLUMN_NAME} IN "
        "('pending_acs_registration', 'pending_service_config')"
    )

    _create_enum_type(_LEGACY_TYPE_NAME, _DOWNGRADE_TYPE_VALUES)

    if _column_type_name(bind, _TABLE_NAME, _COLUMN_NAME) == _TYPE_NAME:
        op.alter_column(
            _TABLE_NAME,
            _COLUMN_NAME,
            existing_type=postgresql.ENUM(name=_TYPE_NAME, create_type=False),
            type_=postgresql.ENUM(
                *_DOWNGRADE_TYPE_VALUES,
                name=_LEGACY_TYPE_NAME,
                create_type=False,
            ),
            postgresql_using=f"{_COLUMN_NAME}::text::{_LEGACY_TYPE_NAME}",
            existing_nullable=True,
        )

    if _type_exists(bind, _TYPE_NAME):
        op.execute(f"DROP TYPE {_TYPE_NAME}")
