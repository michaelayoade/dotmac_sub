"""Add splynx_credit_note_id linkage column to credit_notes.

Enables reconciliation between Splynx credit_notes (legacy) and DotMac
credit_notes for the ongoing migration cleanup.

Revision ID: 102_add_splynx_credit_note_id
Revises: 101_add_internal_note_communication_channel
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "102_add_splynx_credit_note_id"
down_revision = "101_add_internal_note_communication_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "credit_notes",
        sa.Column("splynx_credit_note_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_credit_notes_splynx_credit_note_id",
        "credit_notes",
        ["splynx_credit_note_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_credit_notes_splynx_credit_note_id", table_name="credit_notes")
    op.drop_column("credit_notes", "splynx_credit_note_id")
