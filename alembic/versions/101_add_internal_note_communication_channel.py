"""Add 'internal_note' to communicationchannel and 'internal' to communicationdirection.

Enables import of Splynx customer_notes into communication_logs while preserving
note semantics distinct from email/sms/in_app/whatsapp.

Revision ID: 101_add_internal_note_communication_channel
Revises: 100_add_ont_reconciler_state
Create Date: 2026-05-23
"""

from __future__ import annotations

from alembic import op

revision = "101_add_internal_note_communication_channel"
down_revision = "100_add_ont_reconciler_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE communicationchannel ADD VALUE IF NOT EXISTS 'internal_note'"
    )
    op.execute(
        "ALTER TYPE communicationdirection ADD VALUE IF NOT EXISTS 'internal'"
    )


def downgrade() -> None:
    # PostgreSQL does not support removing enum values directly; leaving in place.
    pass
