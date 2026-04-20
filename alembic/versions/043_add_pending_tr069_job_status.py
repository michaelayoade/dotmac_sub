"""add pending TR-069 job status

Revision ID: 043_add_pending_tr069_job_status
Revises: 042_add_olt_snmp_config
Create Date: 2026-04-20
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from alembic import op

revision = "043_add_pending_tr069_job_status"
down_revision = "042_add_olt_snmp_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE tr069jobstatus ADD VALUE IF NOT EXISTS 'pending'")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("UPDATE tr069_jobs SET status = 'queued' WHERE status = 'pending'")
    op.execute("ALTER TYPE tr069jobstatus RENAME TO tr069jobstatus_old")
    op.execute(
        "CREATE TYPE tr069jobstatus AS ENUM "
        "('queued', 'running', 'succeeded', 'failed', 'canceled')"
    )
    op.alter_column(
        "tr069_jobs",
        "status",
        existing_type=postgresql.ENUM(name="tr069jobstatus_old", create_type=False),
        type_=postgresql.ENUM(
            "queued",
            "running",
            "succeeded",
            "failed",
            "canceled",
            name="tr069jobstatus",
            create_type=False,
        ),
        postgresql_using="status::text::tr069jobstatus",
        existing_nullable=False,
    )
    op.execute("DROP TYPE tr069jobstatus_old")
