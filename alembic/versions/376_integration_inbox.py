"""Add verified inbound integration receipt lifecycle.

Revision ID: 376_integration_inbox
Revises: 375_integration_delivery
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "376_integration_inbox"
down_revision = "375_integration_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_inbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("installation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "capability_binding_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("provider_event_id", sa.String(length=240), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("headers_json", sa.JSON(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("consequence_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('verified', 'processing', 'processed', 'retryable', "
            "'dead_letter')",
            name="ck_integration_inbox_state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_integration_inbox_attempt_count"
        ),
        sa.ForeignKeyConstraint(
            ["capability_binding_id"],
            ["integration_capability_bindings.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["integration_installations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "capability_binding_id",
            "provider_event_id",
            name="uq_integration_inbox_binding_provider_event",
        ),
    )
    op.create_index(
        "ix_integration_inbox_state_received",
        "integration_inbox",
        ["state", "received_at"],
    )


def downgrade() -> None:
    op.drop_table("integration_inbox")
