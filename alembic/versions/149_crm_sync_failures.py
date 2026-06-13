"""CRM push dead-letter: crm_sync_failures.

Records terminal failures of the CRM subscriber-change push (event + batch
paths) so silent drift becomes visible and re-drivable.

Revision ID: 149_crm_sync_failures
Revises: 148_vas_topup_intents
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "149_crm_sync_failures"
down_revision = "148_vas_topup_intents"
branch_labels = None
depends_on = None

_STATUS = sa.Enum("unresolved", "resolved", name="crmsyncfailurestatus")


def upgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("crm_sync_failures"):
        return
    op.create_table(
        "crm_sync_failures",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("entity", sa.String(40), nullable=False, server_default="subscriber"),
        sa.Column("external_id", sa.String(120), nullable=False),
        sa.Column("external_system", sa.String(40), nullable=False),
        sa.Column("payload", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", _STATUS, nullable=False, server_default="unresolved"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_crm_sync_failures_status", "crm_sync_failures", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("crm_sync_failures"):
        op.drop_index("ix_crm_sync_failures_status", table_name="crm_sync_failures")
        op.drop_table("crm_sync_failures")
    _STATUS.drop(bind, checkfirst=True)
