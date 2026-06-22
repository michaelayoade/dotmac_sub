"""Drop obsolete Splynx incremental-sync state tables.

Splynx is decommissioned (local ledger is the sole source of truth), so the
incremental-sync machinery is dead: ``splynx_sync_cursors`` and
``splynx_sync_skips`` have zero references anywhere in ``app/`` — only the
original migration (``150_splynx_incremental_sync_state``) touched them. The
historical Splynx *data* tables (``splynx_billing_transactions``,
``splynx_credit_note_id``, transactions/mappings) are intentionally KEPT — they
are still read by the billing ledger / legacy-BSS views.

Both ops are guarded so the migration is a no-op when the tables are already
absent (fresh DBs, prod divergence). ``downgrade`` recreates the empty table
structures (the dropped sync-cursor/skip rows are not restorable).

Revision ID: 169_drop_splynx_sync_state_tables
Revises: 168_scheduled_task_crontab
Create Date: 2026-06-22
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "169_drop_splynx_sync_state_tables"
down_revision = "168_scheduled_task_crontab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if inspector.has_table("splynx_sync_skips"):
        if any(
            ix["name"] == "ix_splynx_sync_skips_unresolved"
            for ix in inspector.get_indexes("splynx_sync_skips")
        ):
            op.drop_index(
                "ix_splynx_sync_skips_unresolved", table_name="splynx_sync_skips"
            )
        op.drop_table("splynx_sync_skips")
    if inspector.has_table("splynx_sync_cursors"):
        op.drop_table("splynx_sync_cursors")


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table("splynx_sync_cursors"):
        op.create_table(
            "splynx_sync_cursors",
            sa.Column("entity", sa.String(40), primary_key=True),
            sa.Column(
                "last_splynx_id", sa.Integer(), nullable=False, server_default="0"
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
