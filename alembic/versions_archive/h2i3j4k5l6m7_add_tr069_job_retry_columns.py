"""Add retry_count and max_retries to TR-069 jobs.

Revision ID: h2i3j4k5l6m7
Revises: g1h2i3j4k5l6
Create Date: 2026-03-15
"""

import sqlalchemy as sa

from alembic import op

revision = "h2i3j4k5l6m7"
down_revision = "g1h2i3j4k5l6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not inspector.has_table("tr069_jobs"):
        return

    columns = {col["name"] for col in inspector.get_columns("tr069_jobs")}

    if "retry_count" not in columns:
        op.add_column(
            "tr069_jobs",
            sa.Column(
                "retry_count", sa.Integer(), nullable=True, server_default=sa.text("0")
            ),
        )
    if "max_retries" not in columns:
        op.add_column(
            "tr069_jobs",
            sa.Column(
                "max_retries", sa.Integer(), nullable=True, server_default=sa.text("3")
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not inspector.has_table("tr069_jobs"):
        return

    columns = {col["name"] for col in inspector.get_columns("tr069_jobs")}

    if "max_retries" in columns:
        op.drop_column("tr069_jobs", "max_retries")
    if "retry_count" in columns:
        op.drop_column("tr069_jobs", "retry_count")
