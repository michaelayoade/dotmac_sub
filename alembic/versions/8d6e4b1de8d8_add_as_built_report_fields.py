"""Add as-built report fields.

Revision ID: 8d6e4b1de8d8
Revises: 69928eb6e61f
Create Date: 2025-02-14 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "8d6e4b1de8d8"
down_revision = "69928eb6e61f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("as_built_routes", sa.Column("report_file_path", sa.String(length=500), nullable=True))
    op.add_column("as_built_routes", sa.Column("report_file_name", sa.String(length=255), nullable=True))
    op.add_column("as_built_routes", sa.Column("report_generated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("as_built_routes", "report_generated_at")
    op.drop_column("as_built_routes", "report_file_name")
    op.drop_column("as_built_routes", "report_file_path")
