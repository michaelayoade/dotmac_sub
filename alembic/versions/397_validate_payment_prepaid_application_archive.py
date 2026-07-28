"""Validate retired prepaid payment-application evidence already at revision 396.

Revision ID: 397_validate_payment_prepaid_archive
Revises: 396_payment_prepaid_application_archive

Revisions 394 and 396 now validate the archive while upgrading.  This
forward-only migration applies the same complete structural check to databases
that reached revision 396 before that hardening was published.  It writes no
financial or access data and fails closed on missing, legacy, ambiguous, or
malformed evidence state.
"""

from __future__ import annotations

from sqlalchemy import inspect

from alembic import op
from scripts.migration.payment_prepaid_application_archive_schema import (
    ARCHIVE_TABLE,
    LEGACY_TABLE,
    validate_archive_schema,
)

revision = "397_validate_payment_prepaid_archive"
down_revision = "396_payment_prepaid_application_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    legacy_exists = inspector.has_table(LEGACY_TABLE)
    archive_exists = inspector.has_table(ARCHIVE_TABLE)

    if legacy_exists and archive_exists:
        raise RuntimeError(
            "prepaid payment-application evidence is ambiguous: both "
            f"{LEGACY_TABLE} and {ARCHIVE_TABLE} exist"
        )
    if legacy_exists:
        raise RuntimeError(
            "prepaid payment-application retirement is incomplete: legacy table "
            f"{LEGACY_TABLE} still exists"
        )
    if not archive_exists:
        raise RuntimeError(
            "prepaid payment-application evidence is missing: required archive "
            f"{ARCHIVE_TABLE} does not exist"
        )

    validate_archive_schema(bind)


def downgrade() -> None:
    # Forward-only evidence validation has no schema or data change to reverse.
    pass
