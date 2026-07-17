"""Add the subscriber field verification ledger.

Append-only evidence that a subscriber field was confirmed — by whom, when,
from which source, with what proof. No column can carry that; a populated
value proves existence, not confirmation.

Plain create_table: alembic/env.py wraps create-ops to be idempotent, so this
is safe against both a fresh squash-built database (where 001 already built
the table from the model) and an incremental one.

Revision ID: 329_subscriber_field_verifications
Revises: 328_work_order_native_identity
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "329_subscriber_field_verifications"
down_revision = "328_work_order_native_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriber_field_verifications",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_key", sa.String(40), nullable=False),
        sa.Column("value", sa.String(255), nullable=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_by_actor_id", sa.String(120), nullable=True),
        sa.Column("verified_by_actor_name", sa.String(200), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscriber_id"], ["subscribers.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_subscriber_field_verifications_subscriber_id",
        "subscriber_field_verifications",
        ["subscriber_id"],
    )
    op.create_index(
        "ix_subscriber_field_verifications_subscriber_field",
        "subscriber_field_verifications",
        ["subscriber_id", "field_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscriber_field_verifications_subscriber_field",
        table_name="subscriber_field_verifications",
    )
    op.drop_index(
        "ix_subscriber_field_verifications_subscriber_id",
        table_name="subscriber_field_verifications",
    )
    op.drop_table("subscriber_field_verifications")
