"""Backfill targets for Splynx historical usage import.

Adds:
- ``subscriber_daily_usage`` table — daily upload/download volume per
  subscription, sourced from Splynx ``traffic_counter`` (history back to 2018).
- a UNIQUE partial index on ``radius_accounting_sessions.splynx_session_id``
  so the per-session backfill from Splynx ``statistics`` is idempotent and
  cannot double-insert (existing live rows all have NULL splynx_session_id).

Both are additive; no existing data is modified.

Revision ID: 172_splynx_usage_history_import
Revises: 171_system_user_device_login
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "172_splynx_usage_history_import"
down_revision = "171_system_user_device_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriber_daily_usage",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subscription_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id"),
            nullable=True,
        ),
        sa.Column("splynx_service_id", sa.Integer(), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("upload_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "download_bytes", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "source",
            sa.String(40),
            nullable=False,
            server_default="splynx_traffic_counter",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint(
        "uq_subscriber_daily_usage_service_date",
        "subscriber_daily_usage",
        ["splynx_service_id", "usage_date"],
    )
    op.create_index(
        "ix_subscriber_daily_usage_subscription_date",
        "subscriber_daily_usage",
        ["subscription_id", "usage_date"],
    )

    # Idempotency guard for the per-session statistics backfill. Partial unique
    # so it only constrains backfilled rows; live RADIUS rows keep NULL.
    op.create_index(
        "uq_radius_sessions_splynx_session_id",
        "radius_accounting_sessions",
        ["splynx_session_id"],
        unique=True,
        postgresql_where=sa.text("splynx_session_id IS NOT NULL"),
    )
    # The pre-existing non-unique partial index on the same column+predicate is
    # now redundant with the unique one above.
    op.drop_index(
        "idx_radius_sessions_splynx_session_id",
        "radius_accounting_sessions",
        postgresql_where=sa.text("splynx_session_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.create_index(
        "idx_radius_sessions_splynx_session_id",
        "radius_accounting_sessions",
        ["splynx_session_id"],
        postgresql_where=sa.text("splynx_session_id IS NOT NULL"),
    )
    op.drop_index("uq_radius_sessions_splynx_session_id", "radius_accounting_sessions")
    op.drop_index(
        "ix_subscriber_daily_usage_subscription_date", "subscriber_daily_usage"
    )
    op.drop_constraint(
        "uq_subscriber_daily_usage_service_date",
        "subscriber_daily_usage",
        type_="unique",
    )
    op.drop_table("subscriber_daily_usage")
