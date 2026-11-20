"""Persist customer ticket submission identities for exact replay.

Revision ID: 712_customer_ticket_submission_idempotency
Revises: 711_repair_inbox_reply_window_index
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "712_customer_ticket_submission_idempotency"
down_revision: str | None = "711_repair_inbox_reply_window_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "support_tickets",
        sa.Column("customer_submission_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "support_tickets",
        sa.Column("customer_submission_fingerprint", sa.String(64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_support_tickets_customer_submission",
        "support_tickets",
        ["subscriber_id", "customer_submission_id"],
    )
    op.create_check_constraint(
        "ck_support_tickets_customer_submission_evidence",
        "support_tickets",
        "customer_submission_id IS NULL OR "
        "(subscriber_id IS NOT NULL AND customer_submission_fingerprint IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_support_tickets_customer_submission_evidence",
        "support_tickets",
        type_="check",
    )
    op.drop_constraint(
        "uq_support_tickets_customer_submission",
        "support_tickets",
        type_="unique",
    )
    op.drop_column("support_tickets", "customer_submission_fingerprint")
    op.drop_column("support_tickets", "customer_submission_id")
