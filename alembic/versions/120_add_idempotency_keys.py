"""Add idempotency_keys for wallet-affecting customer operations.

Revision ID: 120_add_idempotency_keys
Revises: 119_add_autopay_mandates
Create Date: 2026-06-08

A (scope, key) unique table so a retried money-moving request (e.g. an add-on
purchase) is detected as a replay instead of charging the wallet twice.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "120_add_idempotency_keys"
down_revision = "119_add_autopay_mandates"
branch_labels = None
depends_on = None

_TABLE = "idempotency_keys"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in inspect(bind).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope", sa.String(length=60), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ref_id", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
