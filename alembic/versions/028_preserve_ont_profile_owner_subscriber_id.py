"""Preserve ont_provisioning_profiles.owner_subscriber_id.

Part of DCP-10 (OLT/ONT standalone decoupling): ONT provisioning profiles
keep an optional business-account owner reference for display and ownership
context while network-domain services avoid importing subscriber-domain models.
This migration is defensive: it restores the nullable owner column, FK, and
legacy owner/name uniqueness constraint if an environment has already lost them.

Revision ID: 028_preserve_profile_owner
Revises: 027_ip_subscriber_nullable
Create Date: 2026-04-17

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "028_preserve_profile_owner"
down_revision = "027_ip_subscriber_nullable"
branch_labels = None
depends_on = None


_TABLE = "ont_provisioning_profiles"
_COLUMN = "owner_subscriber_id"
_UNIQUE_CONSTRAINT_NAME = "uq_ont_prov_profiles_owner_name"
_FK_NAME = "ont_provisioning_profiles_owner_subscriber_id_fkey"


def _get_column(inspector: sa.Inspector, column_name: str) -> dict | None:
    for col in inspector.get_columns(_TABLE):
        if col["name"] == column_name:
            return col
    return None


def _find_fk_for_column(inspector: sa.Inspector) -> dict | None:
    for fk in inspector.get_foreign_keys(_TABLE):
        if fk.get("constrained_columns") == [_COLUMN]:
            return fk
    return None


def _find_unique_constraint(
    inspector: sa.Inspector, columns: list[str]
) -> dict | None:
    for uc in inspector.get_unique_constraints(_TABLE):
        if uc.get("column_names") == columns:
            return uc
    return None


def upgrade() -> None:
    """Ensure owner_subscriber_id relationship metadata remains present."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    column = _get_column(inspector, _COLUMN)
    if column is None:
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, postgresql.UUID(as_uuid=True), nullable=True),
        )

    inspector = sa.inspect(conn)
    fk = _find_fk_for_column(inspector)
    if fk is None:
        op.create_foreign_key(
            _FK_NAME,
            _TABLE,
            "subscribers",
            [_COLUMN],
            ["id"],
        )

    inspector = sa.inspect(conn)
    uc = _find_unique_constraint(inspector, [_COLUMN, "name"])
    if uc is None:
        op.create_unique_constraint(
            _UNIQUE_CONSTRAINT_NAME,
            _TABLE,
            [_COLUMN, "name"],
        )


def downgrade() -> None:
    """No-op: downgrading must not drop profile owner relationship data."""
