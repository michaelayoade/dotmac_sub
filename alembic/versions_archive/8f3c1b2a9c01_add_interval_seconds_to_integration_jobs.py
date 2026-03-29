"""add interval seconds to integration jobs

Revision ID: 8f3c1b2a9c01
Revises: 3a7f1d2c9e41
Create Date: 2026-01-13 15:45:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "8f3c1b2a9c01"
down_revision = "3a7f1d2c9e41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("integration_jobs")}
    if "interval_seconds" not in columns:
        op.add_column("integration_jobs", sa.Column("interval_seconds", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("integration_jobs", "interval_seconds")
