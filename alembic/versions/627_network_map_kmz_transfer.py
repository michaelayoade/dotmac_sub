"""Add idempotent Network Map KMZ admission and transfer permissions.

Revision ID: 627_network_map_kmz_transfer
Revises: 626_automation_script_control_plane
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "627_network_map_kmz_transfer"
down_revision: str | None = "626_automation_script_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "fiber_topology_source_batches"
PERMISSIONS = {
    "network:fiber:import": "Import KMZ evidence into the governed fiber staging workflow",
    "network:map:export": "Export authorized Network Map layers as KMZ",
}


def _permission_id(bind: sa.Connection, key: str) -> object | None:
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
    ).scalar()


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.add_column(TABLE, sa.Column("command_key_sha256", sa.String(length=64)))
    op.add_column(TABLE, sa.Column("command_fingerprint_sha256", sa.String(length=64)))
    op.create_check_constraint(
        "ck_fiber_topology_batch_command_key_sha256",
        TABLE,
        "command_key_sha256 IS NULL OR length(command_key_sha256) = 64",
    )
    op.create_check_constraint(
        "ck_fiber_topology_batch_command_fingerprint_sha256",
        TABLE,
        "command_fingerprint_sha256 IS NULL OR length(command_fingerprint_sha256) = 64",
    )
    op.create_unique_constraint(
        "uq_fiber_topology_batch_command_key", TABLE, ["command_key_sha256"]
    )

    bind = op.get_bind()
    if "permissions" not in sa.inspect(bind).get_table_names():
        return
    now = datetime.now(UTC)
    for key, description in PERMISSIONS.items():
        if _permission_id(bind, key) is not None:
            continue
        bind.execute(
            sa.text(
                """
                INSERT INTO permissions (
                    id, key, description, is_active, is_ui_assignable,
                    created_at, updated_at
                )
                VALUES (:id, :key, :description, true, true, :now, :now)
                """
            ),
            {
                "id": str(uuid4()),
                "key": key,
                "description": description,
                "now": now,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" in tables:
        for key in PERMISSIONS:
            permission_id = _permission_id(bind, key)
            if permission_id is None:
                continue
            for table in (
                "role_permissions",
                "subscriber_permissions",
                "system_user_permissions",
            ):
                if table in tables:
                    bind.execute(
                        sa.text(f"DELETE FROM {table} WHERE permission_id = :id"),
                        {"id": permission_id},
                    )
            bind.execute(
                sa.text("DELETE FROM permissions WHERE key = :key"), {"key": key}
            )
    op.drop_constraint("uq_fiber_topology_batch_command_key", TABLE, type_="unique")
    op.drop_constraint(
        "ck_fiber_topology_batch_command_fingerprint_sha256",
        TABLE,
        type_="check",
    )
    op.drop_constraint(
        "ck_fiber_topology_batch_command_key_sha256", TABLE, type_="check"
    )
    op.drop_column(TABLE, "command_fingerprint_sha256")
    op.drop_column(TABLE, "command_key_sha256")
