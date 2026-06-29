"""Add subscription.last_seen_framed_ipv4/ipv6 (observed-IP split).

Splits the OBSERVED framed address (from live RADIUS accounting) out of the
ipv4_address/ipv6_address columns, which are the DESIRED/served IP owned by the
IP assignment + connectivity reconciler. Keeping observed separate stops the
live IP overwriting the desired IP and being re-emitted by the RADIUS sweep.
See docs/designs/CONNECTIVITY_STATE_MACHINE.md §3.1. Nullable, no backfill.

Revision ID: 173_subscription_last_seen_framed_ip
Revises: 172_splynx_usage_history_import
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "173_subscription_last_seen_framed_ip"
down_revision = "172_splynx_usage_history_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("last_seen_framed_ipv4", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("last_seen_framed_ipv6", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "last_seen_framed_ipv6")
    op.drop_column("subscriptions", "last_seen_framed_ipv4")
