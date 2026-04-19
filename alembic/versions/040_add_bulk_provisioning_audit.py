"""add bulk provisioning audit tables

Revision ID: 040_add_bulk_provisioning_audit
Revises: 039_unique_active_ont_normalized_serial
Create Date: 2026-04-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "040_add_bulk_provisioning_audit"
down_revision = "039_unique_active_ont_normalized_serial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_ont_provisioning_events_correlation_key
        ON ont_provisioning_events (correlation_key)
        """
    )
    if "bulk_provisioning_runs" in inspector.get_table_names():
        return

    run_status = postgresql.ENUM(
        "pending",
        "running",
        "succeeded",
        "partial",
        "failed",
        name="bulkprovisioningrunstatus",
        create_type=False,
    )
    item_status = postgresql.ENUM(
        "pending",
        "running",
        "succeeded",
        "failed",
        "skipped",
        name="bulkprovisioningitemstatus",
        create_type=False,
    )
    run_status.create(conn, checkfirst=True)
    item_status.create(conn, checkfirst=True)

    op.create_table(
        "bulk_provisioning_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=True),
        sa.Column("status", run_status, nullable=False, server_default="pending"),
        sa.Column("correlation_key", sa.String(length=256), nullable=False),
        sa.Column("initiated_by", sa.String(length=128), nullable=True),
        sa.Column("max_workers", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("run_metadata", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["profile_id"], ["ont_provisioning_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_bulk_provisioning_runs_status",
        "bulk_provisioning_runs",
        ["status"],
    )
    op.create_index(
        "ix_bulk_provisioning_runs_correlation_key",
        "bulk_provisioning_runs",
        ["correlation_key"],
    )
    op.create_index(
        "ix_bulk_provisioning_runs_started_at",
        "bulk_provisioning_runs",
        ["started_at"],
    )

    op.create_table(
        "bulk_provisioning_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("requested_ont_id", sa.String(length=64), nullable=False),
        sa.Column("ont_unit_id", sa.UUID(), nullable=True),
        sa.Column("status", item_status, nullable=False, server_default="pending"),
        sa.Column("correlation_key", sa.String(length=256), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result_data", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["bulk_provisioning_runs.id"]),
        sa.ForeignKeyConstraint(["ont_unit_id"], ["ont_units.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "requested_ont_id",
            name="uq_bulk_provisioning_items_run_requested_ont",
        ),
    )
    op.create_index(
        "ix_bulk_provisioning_items_run",
        "bulk_provisioning_items",
        ["run_id"],
    )
    op.create_index(
        "ix_bulk_provisioning_items_ont_unit",
        "bulk_provisioning_items",
        ["ont_unit_id"],
    )
    op.create_index(
        "ix_bulk_provisioning_items_status",
        "bulk_provisioning_items",
        ["status"],
    )
    op.create_index(
        "ix_bulk_provisioning_items_correlation_key",
        "bulk_provisioning_items",
        ["correlation_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_bulk_provisioning_items_correlation_key",
        table_name="bulk_provisioning_items",
    )
    op.drop_index(
        "ix_bulk_provisioning_items_status",
        table_name="bulk_provisioning_items",
    )
    op.drop_index(
        "ix_bulk_provisioning_items_ont_unit",
        table_name="bulk_provisioning_items",
    )
    op.drop_index(
        "ix_bulk_provisioning_items_run",
        table_name="bulk_provisioning_items",
    )
    op.drop_table("bulk_provisioning_items")
    op.drop_index(
        "ix_bulk_provisioning_runs_started_at",
        table_name="bulk_provisioning_runs",
    )
    op.drop_index(
        "ix_bulk_provisioning_runs_correlation_key",
        table_name="bulk_provisioning_runs",
    )
    op.drop_index(
        "ix_bulk_provisioning_runs_status",
        table_name="bulk_provisioning_runs",
    )
    op.drop_table("bulk_provisioning_runs")
