"""Retire prepaid payment-application runtime while preserving its evidence.

Revision ID: 394_retire_payment_prepaid_applications
Revises: 393_prepaid_coverage_reconciliation

The runtime model and writers are retired, but rows in this table are historical
financial and access evidence.  Rename the physical table into an archive in one
transaction instead of copying or deleting it.  PostgreSQL's table rename takes
an ACCESS EXCLUSIVE lock, so the deployment migration lock/statement budgets
apply; a lock failure is retried only by retrying the whole migration.

Production inspection on 2026-07-21 found one row.  The rename preserves the
table object, constraints, indexes, and every value exactly.  Finance operations
is the archive steward.  The archive has no application model or writer and is
retained until a separately reviewed retention decision approves deletion.  A
missing, ambiguous, or structurally unverified table state fails closed.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from scripts.migration.payment_prepaid_application_archive_schema import (
    ARCHIVE_TABLE,
    LEGACY_TABLE,
    validate_archive_schema,
)

revision = "394_retire_payment_prepaid_applications"
down_revision = "393_prepaid_coverage_reconciliation"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _row_count(bind, table_name: str) -> int:
    table = sa.Table(table_name, sa.MetaData(), autoload_with=bind)
    return int(bind.scalar(sa.select(sa.func.count()).select_from(table)) or 0)


def upgrade() -> None:
    bind = op.get_bind()
    legacy_exists = _has_table(bind, LEGACY_TABLE)
    archive_exists = _has_table(bind, ARCHIVE_TABLE)

    if legacy_exists and archive_exists:
        raise RuntimeError(
            "prepaid payment-application retirement is ambiguous: both "
            f"{LEGACY_TABLE} and {ARCHIVE_TABLE} exist"
        )
    if not legacy_exists and not archive_exists:
        raise RuntimeError(
            "prepaid payment-application retirement cannot verify evidence: "
            f"neither {LEGACY_TABLE} nor {ARCHIVE_TABLE} exists"
        )
    if archive_exists:
        # Archive-only can represent an operator-reviewed pre-rename or a
        # replay after a forward-only downgrade. Its complete structure must
        # still prove that it is the retired evidence object.
        validate_archive_schema(bind)
        return

    source_count = _row_count(bind, LEGACY_TABLE)
    op.rename_table(LEGACY_TABLE, ARCHIVE_TABLE)
    validate_archive_schema(bind, expected_row_count=source_count)


def downgrade() -> None:
    # Forward-only authority retirement: downgrade must not restore the archive
    # as a live prepaid service-consumption model or writer.
    pass
