"""Add resume_at to enforcement_locks

Revision ID: 48d94c532a05
Revises: 019_add_vlan_scope_to_ip_pools
Create Date: 2026-04-14 07:48:14.306996

"""

from alembic import op
import sqlalchemy as sa


revision = '48d94c532a05'
down_revision = '019_add_vlan_scope_to_ip_pools'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "enforcement_locks",
        sa.Column("resume_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_enforcement_locks_resume_at",
        "enforcement_locks",
        ["resume_at"],
        postgresql_where=sa.text("is_active = true AND resume_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_enforcement_locks_resume_at", table_name="enforcement_locks")
    op.drop_column("enforcement_locks", "resume_at")
