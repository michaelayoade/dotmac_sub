"""Link confirmed credit applications to their exact ledger evidence.

Revision ID: 293_credit_application_evidence
Revises: 292_merge_lifecycle_schedules_and_tax_point_heads
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "293_credit_application_evidence"
down_revision = "292_merge_lifecycle_schedules_and_tax_point_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "credit_note_applications",
        sa.Column(
            "ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "credit_note_applications",
        sa.Column("preview_fingerprint", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_credit_note_applications_ledger_entry_id",
        "credit_note_applications",
        "ledger_entries",
        ["ledger_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_credit_note_applications_ledger_entry_id",
        "credit_note_applications",
        ["ledger_entry_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_credit_note_applications_ledger_entry_id",
        table_name="credit_note_applications",
    )
    op.drop_constraint(
        "fk_credit_note_applications_ledger_entry_id",
        "credit_note_applications",
        type_="foreignkey",
    )
    op.drop_column("credit_note_applications", "preview_fingerprint")
    op.drop_column("credit_note_applications", "ledger_entry_id")
