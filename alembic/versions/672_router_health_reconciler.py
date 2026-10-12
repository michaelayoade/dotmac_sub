"""Router health reconciler: signal state and CoA outcome counters.

Adds ``router_health_signals`` (per-router compact signal state, written only by
``network.router_health``), ``nas_coa_outcome_buckets`` (hourly RADIUS CoA
outcome counters, written only by ``network.coa_outcome_evidence`` on its own
connection, ADR 0018) and an index that makes "latest snapshot per router" cheap.

``nas_coa_outcome_buckets.nas_device_id`` has no foreign key on purpose: the
out-of-band writer must not take locks that interact with the enforcement
caller's transaction.

Both tables are evidence only. ``downgrade()`` drops them and the index, which
discards that evidence; acceptable before the reconciler is relied on (all
controls ship off, shadow mode).

Revision ID: 672_router_health_reconciler
Revises: 671_customer_connection_type_definitions
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "672_router_health_reconciler"
down_revision: str | None = "671_customer_connection_type_definitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = ("disconnect", "update", "canary")
_DISPOSITIONS = (
    "disconnected",
    "session_not_found",
    "disabled",
    "temporarily_suppressed",
    "missing_nas_host",
    "missing_dictionary",
    "invalid_dictionary",
    "missing_secret",
    "rejected",
    "timeout",
    "transport_error",
    "unexpected_response",
    "sent",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "router_health_signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "router_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("routers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("signal", sa.String(length=40), nullable=False),
        sa.Column("scope", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("last_value", sa.Numeric(18, 4), nullable=True),
        sa.Column("last_text", sa.String(length=80), nullable=True),
        sa.Column("samples", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("first_abnormal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_normal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "consecutive_abnormal", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("boot_marker", sa.String(length=40), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ok', 'abnormal', 'unknown')",
            name="ck_router_health_signals_status",
        ),
        sa.UniqueConstraint(
            "router_id",
            "signal",
            "scope",
            name="uq_router_health_signals_router_signal_scope",
        ),
    )
    op.create_index(
        "ix_router_health_signals_signal_status",
        "router_health_signals",
        ["signal", "status"],
    )
    op.create_table(
        "nas_coa_outcome_buckets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("nas_device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("disposition", sa.String(length=30), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"kind IN ({_quoted(_KINDS)})",
            name="ck_nas_coa_outcome_buckets_kind",
        ),
        sa.CheckConstraint(
            f"disposition IN ({_quoted(_DISPOSITIONS)})",
            name="ck_nas_coa_outcome_buckets_disposition",
        ),
        sa.UniqueConstraint(
            "nas_device_id",
            "kind",
            "disposition",
            "bucket_start",
            name="uq_nas_coa_outcome_bucket",
        ),
    )
    op.create_index(
        "ix_nas_coa_outcome_buckets_bucket_start",
        "nas_coa_outcome_buckets",
        ["bucket_start"],
    )
    op.create_index(
        "ix_router_config_snapshots_router_created",
        "router_config_snapshots",
        ["router_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_router_config_snapshots_router_created",
        table_name="router_config_snapshots",
    )
    op.drop_index(
        "ix_nas_coa_outcome_buckets_bucket_start", table_name="nas_coa_outcome_buckets"
    )
    op.drop_table("nas_coa_outcome_buckets")
    op.drop_index(
        "ix_router_health_signals_signal_status", table_name="router_health_signals"
    )
    op.drop_table("router_health_signals")
