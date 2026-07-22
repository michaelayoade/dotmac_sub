"""Ensure the retired prepaid payment-application archive exists.

Revision ID: 396_payment_prepaid_application_archive
Revises: 395_provider_event_provenance

Revision 394 now renames the legacy table so every row survives unchanged.
This compatibility revision creates the same empty archive shape only for a
database that had already applied the earlier empty-table-only form of revision
394 before that correction.  A database with evidence could not have passed the
earlier fail-closed gate, so this branch cannot conceal deleted rows.

The archive has no application model or writer.  Finance operations owns its
retention, and physical deletion requires a separate reviewed decision.  Any
existing archive is accepted only after complete structural validation.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from scripts.migration.payment_prepaid_application_archive_schema import (
    ARCHIVE_TABLE,
    INDEX_CONTRACTS,
    LEGACY_TABLE,
    archive_table_elements,
    validate_archive_schema,
)

revision = "396_payment_prepaid_application_archive"
down_revision = "395_provider_event_provenance"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def upgrade() -> None:
    bind = op.get_bind()
    legacy_exists = _has_table(bind, LEGACY_TABLE)
    archive_exists = _has_table(bind, ARCHIVE_TABLE)
    if legacy_exists:
        archive_state = "also exists" if archive_exists else "is missing"
        raise RuntimeError(
            "prepaid payment-application archive compatibility is ambiguous: "
            f"{LEGACY_TABLE} still exists and {ARCHIVE_TABLE} {archive_state}; "
            "revision 394 must retire the legacy table first"
        )
    if archive_exists:
        validate_archive_schema(bind)
        return

    op.create_table(ARCHIVE_TABLE, *archive_table_elements())
    for index_name, columns, unique in INDEX_CONTRACTS:
        op.create_index(
            index_name,
            ARCHIVE_TABLE,
            list(columns),
            unique=unique,
        )
    validate_archive_schema(bind, expected_row_count=0)


def downgrade() -> None:
    # Forward-only evidence retention: never drop the archive on downgrade.
    pass
