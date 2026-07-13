"""Enforce one applied import per validated source run.

Revision ID: 276_financial_import_sot
Revises: 275_mikrotik_control_plane_alignment
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "276_financial_import_sot"
down_revision = "275_mikrotik_control_plane_alignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("import_runs")}
    if "source_run_id" not in columns:
        op.add_column(
            "import_runs",
            sa.Column(
                "source_run_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )
    inspector = sa.inspect(bind)
    source_fks = [
        fk
        for fk in inspector.get_foreign_keys("import_runs")
        if fk.get("constrained_columns") == ["source_run_id"]
    ]
    if not source_fks:
        op.create_foreign_key(
            "fk_import_runs_source_run_id",
            "import_runs",
            "import_runs",
            ["source_run_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    indexes = {index["name"] for index in inspector.get_indexes("import_runs")}
    if "uq_import_runs_source_run_id" not in indexes:
        op.create_index(
            "uq_import_runs_source_run_id",
            "import_runs",
            ["source_run_id"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("import_runs")}
    if "source_run_id" in columns:
        indexes = {index["name"] for index in inspector.get_indexes("import_runs")}
        if "uq_import_runs_source_run_id" in indexes:
            op.drop_index("uq_import_runs_source_run_id", table_name="import_runs")
        for fk in inspector.get_foreign_keys("import_runs"):
            if fk.get("constrained_columns") == ["source_run_id"] and fk.get("name"):
                op.drop_constraint(fk["name"], "import_runs", type_="foreignkey")
        op.drop_column("import_runs", "source_run_id")
