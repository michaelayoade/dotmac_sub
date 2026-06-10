"""Bank-transfer payment proofs (upload -> verify -> credit).

Revision ID: 135_add_payment_proofs
Revises: 134_add_reseller_service_requests
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "135_add_payment_proofs"
down_revision = "134_add_reseller_service_requests"
branch_labels = None
depends_on = None

_TABLE = "payment_proofs"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _TABLE in inspect(bind).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "submitted_by",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=True,
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), server_default="NGN"),
        sa.Column("bank_name", sa.String(120)),
        sa.Column("reference", sa.String(160)),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("file_path", sa.String(500), nullable=False),
        sa.Column(
            "status",
            sa.Enum("submitted", "verified", "rejected", name="paymentproofstatus"),
            nullable=False,
            server_default="submitted",
            index=True,
        ),
        sa.Column("review_notes", sa.Text),
        sa.Column("verified_by", sa.String(120)),
        sa.Column(
            "payment_id", UUID(as_uuid=True), sa.ForeignKey("payments.id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_table(_TABLE)
    sa.Enum(name="paymentproofstatus").drop(bind, checkfirst=True)
