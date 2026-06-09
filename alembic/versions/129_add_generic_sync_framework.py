"""Add generic sync framework fields and record history.

Revision ID: 129_add_generic_sync_framework
Revises: 128_add_device_tokens
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "129_add_generic_sync_framework"
down_revision = "128_add_device_tokens"
branch_labels = None
depends_on = None


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns(table)}
    if column.name not in columns:
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    _add_column_if_missing("integration_jobs", sa.Column("adapter_key", sa.String(80)))
    _add_column_if_missing("integration_jobs", sa.Column("action", sa.String(80)))
    _add_column_if_missing("integration_jobs", sa.Column("entity_type", sa.String(80)))
    _add_column_if_missing("integration_jobs", sa.Column("direction", sa.String(24)))
    _add_column_if_missing("integration_jobs", sa.Column("trigger_mode", sa.String(24)))
    _add_column_if_missing("integration_jobs", sa.Column("mapping_config", sa.JSON()))
    _add_column_if_missing("integration_jobs", sa.Column("filter_config", sa.JSON()))
    _add_column_if_missing("integration_jobs", sa.Column("conflict_policy", sa.String(40)))
    _add_column_if_missing("integration_runs", sa.Column("trigger", sa.String(32)))
    _add_column_if_missing("integration_runs", sa.Column("requested_by", sa.String(160)))

    inspector = inspect(bind)
    if "integration_records" not in inspector.get_table_names():
        op.create_table(
            "integration_records",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "run_id",
                UUID(as_uuid=True),
                sa.ForeignKey("integration_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("entity_type", sa.String(80), nullable=False),
            sa.Column("direction", sa.String(24), nullable=False),
            sa.Column("local_id", sa.String(120)),
            sa.Column("remote_id", sa.String(120)),
            sa.Column("remote_number", sa.String(120)),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("status", sa.String(40), nullable=False),
            sa.Column("reason", sa.Text()),
            sa.Column("payload_snapshot", sa.JSON()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_integration_records_run_id", "integration_records", ["run_id"]
        )
        op.create_index(
            "ix_integration_records_remote",
            "integration_records",
            ["entity_type", "remote_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    inspector = inspect(bind)
    if "integration_records" in inspector.get_table_names():
        op.drop_index("ix_integration_records_remote", table_name="integration_records")
        op.drop_index("ix_integration_records_run_id", table_name="integration_records")
        op.drop_table("integration_records")

    for table, columns in {
        "integration_runs": ["requested_by", "trigger"],
        "integration_jobs": [
            "conflict_policy",
            "filter_config",
            "mapping_config",
            "trigger_mode",
            "direction",
            "entity_type",
            "action",
            "adapter_key",
        ],
    }.items():
        if table not in inspector.get_table_names():
            continue
        existing = {item["name"] for item in inspector.get_columns(table)}
        for column in columns:
            if column in existing:
                op.drop_column(table, column)
