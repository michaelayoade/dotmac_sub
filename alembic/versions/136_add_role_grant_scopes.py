"""Object-scoped role grants: scope_type / scope_id on role assignments.

A staff/subscriber role grant can now be scoped to a region or reseller.
Empty strings preserve the historical GLOBAL (unscoped) behaviour, so every
existing grant keeps full reach. The unique constraint widens to include the
scope so the same role can be granted at several scopes.

Revision ID: 136_add_role_grant_scopes
Revises: 135_add_payment_proofs
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "136_add_role_grant_scopes"
down_revision = "135_add_payment_proofs"
branch_labels = None
depends_on = None

_TABLES = {
    "subscriber_roles": "uq_subscriber_roles_subscriber_role",
    "system_user_roles": "uq_system_user_roles_user_role",
}
_ID_COL = {
    "subscriber_roles": "subscriber_id",
    "system_user_roles": "system_user_id",
}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    inspector = inspect(bind)
    for table, uq_name in _TABLES.items():
        if table not in inspector.get_table_names():
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "scope_type" not in cols:
            op.add_column(
                table,
                sa.Column(
                    "scope_type", sa.String(20), nullable=False, server_default=""
                ),
            )
        if "scope_id" not in cols:
            op.add_column(
                table,
                sa.Column("scope_id", sa.String(64), nullable=False, server_default=""),
            )
        existing = {c["name"] for c in inspector.get_unique_constraints(table)}
        if uq_name in existing:
            op.drop_constraint(uq_name, table, type_="unique")
        op.create_unique_constraint(
            uq_name,
            table,
            [_ID_COL[table], "role_id", "scope_type", "scope_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    for table, uq_name in _TABLES.items():
        op.drop_constraint(uq_name, table, type_="unique")
        op.create_unique_constraint(uq_name, table, [_ID_COL[table], "role_id"])
        op.drop_column(table, "scope_id")
        op.drop_column(table, "scope_type")
