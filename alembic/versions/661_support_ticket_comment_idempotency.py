"""Add support ticket comment idempotency.

Revision ID: 661_support_ticket_comment_idempotency
Revises: 658_regional_report_billing_indexes
Create Date: 2026-10-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "661_support_ticket_comment_idempotency"
down_revision: str | None = "658_regional_report_billing_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "support_ticket_comments",
        sa.Column(
            "idempotency_key",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "support_ticket_comments",
        sa.Column("idempotency_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_support_ticket_comments_ticket_idempotency_key",
        "support_ticket_comments",
        ["ticket_id", "idempotency_key"],
    )
    op.create_check_constraint(
        "ck_support_ticket_comments_idempotency_evidence",
        "support_ticket_comments",
        "(idempotency_key IS NULL) = (idempotency_fingerprint IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_support_ticket_comments_idempotency_evidence",
        "support_ticket_comments",
        type_="check",
    )
    op.drop_constraint(
        "uq_support_ticket_comments_ticket_idempotency_key",
        "support_ticket_comments",
        type_="unique",
    )
    op.drop_column("support_ticket_comments", "idempotency_key")
    op.drop_column("support_ticket_comments", "idempotency_fingerprint")
