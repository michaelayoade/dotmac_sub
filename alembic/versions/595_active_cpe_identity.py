"""Enforce one active TR-069 identity per CPE inventory record.

The CPE-detail WiFi owner resolves an exact active ``Tr069CpeDevice`` by
``cpe_device_id``. Application-level ambiguity checks are insufficient under
concurrency: an inactive sibling could be activated after the owner's read.
This partial unique index makes the invariant atomic at the database boundary.

The migration deliberately does not choose a winner for existing duplicates.
It reports a bounded UUID-only sample and refuses until an operator adjudicates
which identity is authoritative.

Revision ID: 595_active_cpe_identity
Revises: 594_field_expense_destination
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "595_active_cpe_identity"
down_revision = "594_field_expense_destination"
branch_labels = None
depends_on = None

_INDEX = "uq_tr069_cpe_devices_active_cpe_device_id"


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(
            "SELECT cpe_device_id, count(*) AS active_count "
            "FROM tr069_cpe_devices "
            "WHERE is_active AND cpe_device_id IS NOT NULL "
            "GROUP BY cpe_device_id HAVING count(*) > 1 "
            "ORDER BY cpe_device_id LIMIT 10"
        )
    ).fetchall()
    if duplicates:
        sample = ", ".join(f"{row[0]} ({row[1]})" for row in duplicates)
        raise RuntimeError(
            "Cannot enforce one active TR-069 identity per CPE while duplicate "
            f"active links exist: {sample}. Deactivate the non-authoritative "
            "rows after reviewed identity adjudication, then re-run."
        )

    op.create_index(
        _INDEX,
        "tr069_cpe_devices",
        ["cpe_device_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND cpe_device_id IS NOT NULL"),
        sqlite_where=sa.text("is_active AND cpe_device_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="tr069_cpe_devices")
