"""Add resume_at to enforcement_locks

Revision ID: 48d94c532a05
Revises: 019_add_vlan_scope_to_ip_pools
Create Date: 2026-04-14 07:48:14.306996

"""

import sqlalchemy as sa

from alembic import op

revision = "48d94c532a05"
down_revision = "019_add_vlan_scope_to_ip_pools"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def _index_exists(table_name: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _column_exists("enforcement_locks", "resume_at"):
        op.add_column(
            "enforcement_locks",
            sa.Column("resume_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _index_exists("enforcement_locks", "ix_enforcement_locks_resume_at"):
        op.create_index(
            "ix_enforcement_locks_resume_at",
            "enforcement_locks",
            ["resume_at"],
            postgresql_where=sa.text("is_active = true AND resume_at IS NOT NULL"),
        )


def downgrade() -> None:
    if _index_exists("enforcement_locks", "ix_enforcement_locks_resume_at"):
        op.drop_index("ix_enforcement_locks_resume_at", table_name="enforcement_locks")
    if _column_exists("enforcement_locks", "resume_at"):
        op.drop_column("enforcement_locks", "resume_at")
