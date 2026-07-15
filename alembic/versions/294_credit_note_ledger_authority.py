"""Link credit-note lifecycle postings to their source documents.

Revision ID: 294_credit_note_ledger_authority
Revises: 293_cpe_firmware_identity
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "294_credit_note_ledger_authority"
down_revision = "293_cpe_firmware_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ledger_entries",
        sa.Column("credit_note_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "ledger_entries",
        sa.Column("credit_note_application_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ledger_entries_credit_note_id",
        "ledger_entries",
        "credit_notes",
        ["credit_note_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_ledger_entries_credit_note_application_id",
        "ledger_entries",
        "credit_note_applications",
        ["credit_note_application_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_ledger_entries_credit_note_issuance_shape",
        "ledger_entries",
        "credit_note_id IS NULL "
        "OR credit_note_application_id IS NOT NULL "
        "OR reversal_of_entry_id IS NOT NULL "
        "OR (source = 'credit_note' AND entry_type = 'credit' "
        "AND invoice_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_ledger_entries_credit_note_application_shape",
        "ledger_entries",
        "credit_note_application_id IS NULL OR "
        "(credit_note_id IS NOT NULL AND source = 'credit_note' AND "
        "((entry_type = 'debit' AND invoice_id IS NULL) OR "
        "(entry_type = 'credit' AND invoice_id IS NOT NULL)))",
    )
    op.create_index(
        "ix_ledger_entries_credit_note_id",
        "ledger_entries",
        ["credit_note_id"],
    )
    op.create_index(
        "ix_ledger_entries_credit_note_application_id",
        "ledger_entries",
        ["credit_note_application_id"],
    )
    issuance_predicate = sa.text(
        "credit_note_id IS NOT NULL "
        "AND credit_note_application_id IS NULL "
        "AND reversal_of_entry_id IS NULL"
    )
    op.create_index(
        "uq_ledger_entries_credit_note_issuance",
        "ledger_entries",
        ["credit_note_id"],
        unique=True,
        postgresql_where=issuance_predicate,
        sqlite_where=issuance_predicate,
    )
    op.create_index(
        "uq_ledger_entries_credit_note_application_type",
        "ledger_entries",
        ["credit_note_application_id", "entry_type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_ledger_entries_credit_note_application_type",
        table_name="ledger_entries",
    )
    op.drop_index(
        "uq_ledger_entries_credit_note_issuance",
        table_name="ledger_entries",
    )
    op.drop_index(
        "ix_ledger_entries_credit_note_application_id",
        table_name="ledger_entries",
    )
    op.drop_index("ix_ledger_entries_credit_note_id", table_name="ledger_entries")
    op.drop_constraint(
        "ck_ledger_entries_credit_note_application_shape",
        "ledger_entries",
        type_="check",
    )
    op.drop_constraint(
        "ck_ledger_entries_credit_note_issuance_shape",
        "ledger_entries",
        type_="check",
    )
    op.drop_constraint(
        "fk_ledger_entries_credit_note_application_id",
        "ledger_entries",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_ledger_entries_credit_note_id",
        "ledger_entries",
        type_="foreignkey",
    )
    op.drop_column("ledger_entries", "credit_note_application_id")
    op.drop_column("ledger_entries", "credit_note_id")
