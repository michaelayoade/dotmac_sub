"""Add exact ledger evidence for credit-note issue, application, and void.

Revision ID: 294_credit_note_lifecycle_evidence
Revises: 293_credit_application_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "294_credit_note_lifecycle_evidence"
down_revision = "293_credit_application_evidence"
branch_labels = None
depends_on = None


def _uuid_column(name: str) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), nullable=True)


def upgrade() -> None:
    op.add_column("credit_notes", _uuid_column("funding_ledger_entry_id"))
    op.add_column(
        "credit_notes",
        sa.Column("issue_preview_fingerprint", sa.String(64), nullable=True),
    )
    op.add_column("credit_notes", _uuid_column("void_ledger_entry_id"))
    op.add_column(
        "credit_notes",
        sa.Column("void_preview_fingerprint", sa.String(64), nullable=True),
    )
    op.add_column(
        "credit_note_applications",
        _uuid_column("consumption_ledger_entry_id"),
    )

    op.create_foreign_key(
        "fk_credit_notes_funding_ledger_entry_id",
        "credit_notes",
        "ledger_entries",
        ["funding_ledger_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_credit_notes_void_ledger_entry_id",
        "credit_notes",
        "ledger_entries",
        ["void_ledger_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_credit_note_applications_consumption_ledger_entry_id",
        "credit_note_applications",
        "ledger_entries",
        ["consumption_ledger_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_credit_notes_funding_ledger_entry_id",
        "credit_notes",
        ["funding_ledger_entry_id"],
        unique=True,
    )
    op.create_index(
        "uq_credit_notes_void_ledger_entry_id",
        "credit_notes",
        ["void_ledger_entry_id"],
        unique=True,
    )
    op.create_index(
        "uq_credit_note_applications_consumption_ledger_entry_id",
        "credit_note_applications",
        ["consumption_ledger_entry_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_credit_note_applications_consumption_ledger_entry_id",
        table_name="credit_note_applications",
    )
    op.drop_index(
        "uq_credit_notes_void_ledger_entry_id",
        table_name="credit_notes",
    )
    op.drop_index(
        "uq_credit_notes_funding_ledger_entry_id",
        table_name="credit_notes",
    )
    op.drop_constraint(
        "fk_credit_note_applications_consumption_ledger_entry_id",
        "credit_note_applications",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_credit_notes_void_ledger_entry_id",
        "credit_notes",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_credit_notes_funding_ledger_entry_id",
        "credit_notes",
        type_="foreignkey",
    )
    op.drop_column("credit_note_applications", "consumption_ledger_entry_id")
    op.drop_column("credit_notes", "void_preview_fingerprint")
    op.drop_column("credit_notes", "void_ledger_entry_id")
    op.drop_column("credit_notes", "issue_preview_fingerprint")
    op.drop_column("credit_notes", "funding_ledger_entry_id")
