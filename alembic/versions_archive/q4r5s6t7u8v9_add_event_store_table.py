"""Add event_store table for event persistence and retry.

Revision ID: q4r5s6t7u8v9
Revises: 6f1c2d3e4b5a, b2e1f3c4d5a6, p3q4r5s6t7u8
Create Date: 2026-01-22 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "q4r5s6t7u8v9"
down_revision: Union[str, Sequence[str]] = ("6f1c2d3e4b5a", "b2e1f3c4d5a6", "p3q4r5s6t7u8")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create event status enum
    event_status_enum = postgresql.ENUM(
        "pending", "processing", "completed", "failed",
        name="eventstatus",
        create_type=False,
    )
    event_status_enum.create(op.get_bind(), checkfirst=True)

    # Create event_store table
    op.create_table(
        "event_store",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending", "processing", "completed", "failed",
                name="eventstatus",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor", sa.String(255), nullable=True),
        sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("service_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("failed_handlers", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
    )

    # Create indexes
    op.create_index("ix_event_store_event_id", "event_store", ["event_id"], unique=True)
    op.create_index("ix_event_store_event_type", "event_store", ["event_type"])
    op.create_index("ix_event_store_status", "event_store", ["status"])
    op.create_index("ix_event_store_subscriber_id", "event_store", ["subscriber_id"])
    op.create_index("ix_event_store_account_id", "event_store", ["account_id"])
    op.create_index(
        "ix_event_store_status_retry",
        "event_store",
        ["status", "retry_count", "created_at"],
        postgresql_where=sa.text("status = 'failed'"),
    )


def downgrade() -> None:
    # Drop indexes
    op.drop_index("ix_event_store_status_retry", table_name="event_store")
    op.drop_index("ix_event_store_account_id", table_name="event_store")
    op.drop_index("ix_event_store_subscriber_id", table_name="event_store")
    op.drop_index("ix_event_store_status", table_name="event_store")
    op.drop_index("ix_event_store_event_type", table_name="event_store")
    op.drop_index("ix_event_store_event_id", table_name="event_store")

    # Drop table
    op.drop_table("event_store")

    # Drop enum
    postgresql.ENUM(name="eventstatus").drop(op.get_bind(), checkfirst=True)
