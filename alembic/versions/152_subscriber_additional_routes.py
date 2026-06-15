"""Add subscriber_additional_routes for RADIUS Framed-Route emission.

Holds the extra routed IP blocks Splynx used to attach via its
``services_internet.ipv4_route`` field. After the 2026-06-11 RADIUS cutover
dotmac_sub answers Access-Accept and must reproduce these as Framed-Route
attributes; this table is the source the reply builder reads from.

Revision ID: 152_subscriber_additional_routes
Revises: 150_splynx_incremental_sync_state
Create Date: 2026-06-15

Re-parented onto main's head (150_splynx_incremental_sync_state) instead of the
prod-local-only 151_customer_notification_read_at: this migration only creates
an independent table and has no real dependency on 151, so decoupling lets it
land on main without waiting for that separate feature.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "152_subscriber_additional_routes"
down_revision = "150_splynx_incremental_sync_state"
branch_labels = None
depends_on = None

TABLE = "subscriber_additional_routes"


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cidr", sa.String(64), nullable=False),
        sa.Column("prefix_length", sa.Integer(), nullable=False),
        sa.Column("metric", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("source", sa.String(32), nullable=True),
        sa.Column("splynx_service_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "subscriber_id",
            "cidr",
            name="uq_subscriber_additional_routes_subscriber_cidr",
        ),
    )
    op.create_index(
        "ix_subscriber_additional_routes_subscriber_id",
        TABLE,
        ["subscriber_id"],
    )


def downgrade() -> None:
    if not _has_table(TABLE):
        return
    op.drop_index("ix_subscriber_additional_routes_subscriber_id", table_name=TABLE)
    op.drop_table(TABLE)
