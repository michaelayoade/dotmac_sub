"""Record an owner resolution for dispatches stuck in reconciliation_needed.

Adds the terminal ``resolved`` dispatch status and four nullable resolution
columns. Additive: existing rows are untouched and no data is backfilled.

PostgreSQL cannot drop an enum value, so downgrade keeps the value and drops
the columns, but refuses to run while any row is ``resolved`` so resolution
evidence is never silently destroyed.

Revision ID: 699_dispatch_reconciliation_resolution
Revises: 698_credential_deactivation_reason
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "699_dispatch_reconciliation_resolution"
down_revision: str | None = "698_credential_deactivation_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "network_operation_dispatches"
_COLUMNS = (
    ("resolution", "VARCHAR(32)"),
    ("resolution_evidence", "TEXT"),
    ("resolved_by", "VARCHAR(160)"),
    ("resolved_at", "TIMESTAMP WITH TIME ZONE"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE networkoperationdispatchstatus "
            "ADD VALUE IF NOT EXISTS 'resolved'"
        )
    for name, ddl_type in _COLUMNS:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {ddl_type}")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM {_TABLE} WHERE status::text = 'resolved') THEN "
        "RAISE EXCEPTION 'Refusing downgrade: resolved dispatches carry "
        "resolution evidence'; "
        "END IF; END $$"
    )
    for name, _ in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
