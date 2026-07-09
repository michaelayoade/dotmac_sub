"""Add field settings domain.

Revision ID: 224_add_field_setting_domain
Revises: 223_work_order_dispatch_foundation
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op

revision = "224_add_field_setting_domain"
down_revision = "223_work_order_dispatch_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE settingdomain ADD VALUE IF NOT EXISTS 'field'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely without rebuilding the
    # type and rewriting dependent rows. Leaving the value is the least
    # surprising downgrade behavior used by the existing enum migrations.
    pass
