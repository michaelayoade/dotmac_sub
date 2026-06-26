"""IPv6 prefix-delegation: pool delegation size + delegated-prefix inventory

Adds ``ip_pools.delegation_prefix_length`` (size of each customer PD carved from
an IPv6 pool's parent prefix) and the ``ipv6_delegated_prefixes`` inventory table
(one row per delegated prefix, with a reservation state). The app is the source
of truth for who owns which prefix.

Idempotent: the squashed-initial migration builds the schema via
Base.metadata.create_all() from the current model, which already declares these,
so on a fresh DB they exist and this no-ops; on an existing prod DB it adds them.

Revision ID: 178_ipv6_delegated_prefixes
Revises: 177_ipam_partial_active_unique
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "178_ipv6_delegated_prefixes"
down_revision = "177_ipam_partial_active_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if "delegation_prefix_length" not in {
        c["name"] for c in insp.get_columns("ip_pools")
    }:
        op.add_column(
            "ip_pools",
            sa.Column("delegation_prefix_length", sa.Integer(), nullable=True),
        )

    if "ipv6_delegated_prefixes" not in set(insp.get_table_names()):
        op.create_table(
            "ipv6_delegated_prefixes",
            sa.Column(
                "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
            ),
            sa.Column(
                "pool_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("ip_pools.id"),
                nullable=False,
            ),
            sa.Column("prefix", sa.String(length=64), nullable=False),
            sa.Column("prefix_length", sa.Integer(), nullable=False),
            sa.Column(
                "state",
                sa.Enum(
                    "available",
                    "reserved",
                    "assigned",
                    name="ipv6prefixstate",
                ),
                nullable=False,
            ),
            sa.Column(
                "subscriber_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscribers.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "subscription_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "pool_id",
                "prefix",
                "prefix_length",
                name="uq_ipv6_pd_pool_prefix",
            ),
        )
        op.create_index(
            "ix_ipv6_delegated_prefixes_pool_id",
            "ipv6_delegated_prefixes",
            ["pool_id"],
        )
        op.create_index(
            "ix_ipv6_delegated_prefixes_state",
            "ipv6_delegated_prefixes",
            ["state"],
        )
        op.create_index(
            "ix_ipv6_delegated_prefixes_subscriber_id",
            "ipv6_delegated_prefixes",
            ["subscriber_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "ipv6_delegated_prefixes" in set(insp.get_table_names()):
        op.drop_table("ipv6_delegated_prefixes")
        op.execute("DROP TYPE IF EXISTS ipv6prefixstate")
    if "delegation_prefix_length" in {c["name"] for c in insp.get_columns("ip_pools")}:
        op.drop_column("ip_pools", "delegation_prefix_length")
