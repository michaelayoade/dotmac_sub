"""Remove legacy flat TR-069 provisioning step types.

Revision ID: 052_remove_legacy_tr069_step_types
Revises: 051_add_authorization_presets_table
Create Date: 2026-04-23
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "052_remove_legacy_tr069_step_types"
down_revision = "051_add_authorization_presets_table"
branch_labels = None
depends_on = None

_TYPE_NAME = "provisioningsteptype"
_LEGACY_VALUES = ("push_tr069_wan_config", "push_tr069_pppoe_credentials")
_CURRENT_VALUES = (
    "assign_ont",
    "push_config",
    "confirm_up",
    "resolve_profile",
    "push_ont_profile",
    "verify_ont_config",
    "create_olt_service_port",
    "restore_olt_from_backup",
    "ensure_nas_vlan",
)


def _enum_labels(conn) -> set[str]:
    rows = conn.execute(
        sa.text(
            """
            SELECT e.enumlabel
            FROM pg_enum e
            JOIN pg_type t ON t.oid = e.enumtypid
            WHERE t.typname = :type_name
            """
        ),
        {"type_name": _TYPE_NAME},
    ).fetchall()
    return {str(row[0]) for row in rows}


def _recreate_enum_without_legacy_values(conn) -> None:
    labels = _enum_labels(conn)
    if not labels.intersection(_LEGACY_VALUES):
        return

    inspector = inspect(conn)
    if "provisioning_steps" in inspector.get_table_names():
        conn.execute(
            sa.text(
                """
                DELETE FROM provisioning_steps
                WHERE step_type::text IN :legacy_values
                """
            ).bindparams(sa.bindparam("legacy_values", expanding=True)),
            {"legacy_values": _LEGACY_VALUES},
        )
        op.execute(
            "ALTER TABLE provisioning_steps "
            "ALTER COLUMN step_type TYPE text USING step_type::text"
        )

    op.execute(f"DROP TYPE {_TYPE_NAME}")
    values_sql = ", ".join(f"'{value}'" for value in _CURRENT_VALUES)
    op.execute(f"CREATE TYPE {_TYPE_NAME} AS ENUM ({values_sql})")

    if "provisioning_steps" in inspector.get_table_names():
        op.execute(
            "ALTER TABLE provisioning_steps "
            f"ALTER COLUMN step_type TYPE {_TYPE_NAME} "
            f"USING step_type::{_TYPE_NAME}"
        )


def upgrade() -> None:
    _recreate_enum_without_legacy_values(op.get_bind())


def downgrade() -> None:
    for value in _LEGACY_VALUES:
        op.execute(f"ALTER TYPE {_TYPE_NAME} ADD VALUE IF NOT EXISTS '{value}'")
