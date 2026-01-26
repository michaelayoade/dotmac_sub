"""Add attachments to project_task_comments.

Revision ID: ab12cd34ef56
Revises: 7f3c2a9e6c51
Create Date: 2026-01-14 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "ab12cd34ef56"
down_revision = "7f3c2a9e6c51"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("project_task_comments")}
    if "attachments" not in columns:
        op.add_column("project_task_comments", sa.Column("attachments", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("project_task_comments", "attachments")
