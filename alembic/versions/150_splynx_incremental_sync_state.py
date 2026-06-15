"""Add state tables for Splynx incremental sync.

Revision ID: 150_splynx_incremental_sync_state
Revises: 149_crm_sync_failures
Create Date: 2026-06-15
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "150_splynx_incremental_sync_state"
down_revision = "149_crm_sync_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("splynx_sync_cursors"):
        op.create_table(
            "splynx_sync_cursors",
            sa.Column("entity", sa.String(40), primary_key=True),
            sa.Column(
                "last_splynx_id",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("NOW()"),
            ),
        )

    if not inspector.has_table("splynx_sync_skips"):
        op.create_table(
            "splynx_sync_skips",
            sa.Column(
                "id",
                UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("entity", sa.String(40), nullable=False),
            sa.Column("splynx_id", sa.Integer(), nullable=False),
            sa.Column("customer_id", sa.Integer()),
            sa.Column("reason", sa.String(80), nullable=False),
            sa.Column("payload", JSONB()),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "first_seen_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("NOW()"),
            ),
            sa.Column(
                "last_seen_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("NOW()"),
            ),
            sa.Column("resolved_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "entity", "splynx_id", name="uq_splynx_sync_skips_entity_id"
            ),
        )
        op.create_index(
            "ix_splynx_sync_skips_unresolved",
            "splynx_sync_skips",
            ["entity", "resolved_at", "splynx_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("splynx_sync_skips"):
        op.drop_index("ix_splynx_sync_skips_unresolved", table_name="splynx_sync_skips")
        op.drop_table("splynx_sync_skips")
    if inspector.has_table("splynx_sync_cursors"):
        op.drop_table("splynx_sync_cursors")
