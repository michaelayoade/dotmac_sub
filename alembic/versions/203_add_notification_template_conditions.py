"""Add conditions JSON to notification templates.

Revision ID: 203_add_notification_template_conditions
Revises: 202_index_service_extension_entries
Create Date: 2026-07-03
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "203_add_notification_template_conditions"
down_revision = "202_index_service_extension_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    column_type = postgresql.JSONB() if is_pg else sa.JSON()
    default = sa.text("'{}'::jsonb") if is_pg else sa.text("'{}'")
    op.add_column(
        "notification_templates",
        sa.Column(
            "conditions",
            column_type,
            nullable=False,
            server_default=default,
        ),
    )
    op.alter_column("notification_templates", "conditions", server_default=None)


def downgrade() -> None:
    op.drop_column("notification_templates", "conditions")
