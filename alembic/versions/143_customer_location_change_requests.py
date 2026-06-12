"""Create customer_location_change_requests.

The model and service layer arrived in the original backup-module copy but
were never wired or migrated; this creates the table for the now-exposed
customer Service Location page (pin-correction requests with admin review).

Revision ID: 143_customer_location_change_requests
Revises: 142_perf_hot_page_indexes
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "143_customer_location_change_requests"
down_revision = "142_perf_hot_page_indexes"
branch_labels = None
depends_on = None

TABLE = "customer_location_change_requests"

_STATUS = sa.Enum(
    "pending",
    "approved",
    "rejected",
    "cancelled",
    name="customerlocationchangerequeststatus",
)


def upgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=False,
        ),
        sa.Column("address_id", UUID(as_uuid=True), sa.ForeignKey("addresses.id")),
        sa.Column("current_latitude", sa.Float()),
        sa.Column("current_longitude", sa.Float()),
        sa.Column("requested_latitude", sa.Float(), nullable=False),
        sa.Column("requested_longitude", sa.Float(), nullable=False),
        sa.Column("customer_note", sa.Text()),
        sa.Column("status", _STATUS, nullable=False, server_default="pending"),
        sa.Column("reviewed_by_actor_id", sa.String(120)),
        sa.Column("reviewed_by_actor_name", sa.String(200)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_note", sa.Text()),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("submitted_from_ip", sa.String(64)),
        sa.Column("metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_customer_location_change_requests_subscriber_id",
        TABLE,
        ["subscriber_id"],
    )
    op.create_index(
        "ix_customer_location_change_requests_status",
        TABLE,
        ["status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table(TABLE):
        return
    op.drop_index("ix_customer_location_change_requests_status", table_name=TABLE)
    op.drop_index(
        "ix_customer_location_change_requests_subscriber_id", table_name=TABLE
    )
    op.drop_table(TABLE)
    _STATUS.drop(bind, checkfirst=True)
