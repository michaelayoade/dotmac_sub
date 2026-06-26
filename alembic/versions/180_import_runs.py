"""Durable bulk-import run/row tracking (import_runs, import_run_rows)

Idempotent: the squashed-initial migration builds the schema from the current
model via create_all(), so on a fresh DB these tables already exist and this
no-ops; on an existing prod DB it creates them.

Revision ID: 180_import_runs
Revises: 179_billing_always_on
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "180_import_runs"
down_revision = "179_billing_always_on"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "import_runs" not in tables:
        op.create_table(
            "import_runs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("module", sa.String(length=60), nullable=False),
            sa.Column(
                "status",
                sa.Enum(
                    "pending",
                    "running",
                    "dry_run_ready",
                    "completed",
                    "failed",
                    name="importrunstatus",
                ),
                nullable=False,
            ),
            sa.Column("dry_run", sa.Boolean(), nullable=False),
            sa.Column("data_format", sa.String(length=20), nullable=False),
            sa.Column("source_name", sa.String(length=255), nullable=True),
            sa.Column("csv_delimiter", sa.String(length=4), nullable=False),
            sa.Column("column_mapping", sa.JSON(), nullable=True),
            sa.Column("input_text", sa.Text(), nullable=True),
            sa.Column("total_rows", sa.Integer(), nullable=False),
            sa.Column("ok_rows", sa.Integer(), nullable=False),
            sa.Column("failed_rows", sa.Integer(), nullable=False),
            sa.Column("skipped_rows", sa.Integer(), nullable=False),
            sa.Column("created_by", sa.String(length=120), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("summary", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_import_runs_status", "import_runs", ["status"])
        op.create_index("ix_import_runs_module", "import_runs", ["module"])
        op.create_index("ix_import_runs_created_at", "import_runs", ["created_at"])

    if "import_run_rows" not in tables:
        op.create_table(
            "import_run_rows",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "run_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("import_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("row_number", sa.Integer(), nullable=False),
            sa.Column("raw", sa.JSON(), nullable=True),
            sa.Column(
                "status",
                sa.Enum(
                    "pending",
                    "ok",
                    "error",
                    "skipped",
                    name="importrowstatus",
                ),
                nullable=False,
            ),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "run_id", "row_number", name="uq_import_run_rows_run_line"
            ),
        )
        op.create_index("ix_import_run_rows_run", "import_run_rows", ["run_id"])
        op.create_index("ix_import_run_rows_status", "import_run_rows", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "import_run_rows" in tables:
        op.drop_table("import_run_rows")
    if "import_runs" in tables:
        op.drop_table("import_runs")
    op.execute("DROP TYPE IF EXISTS importrowstatus")
    op.execute("DROP TYPE IF EXISTS importrunstatus")
