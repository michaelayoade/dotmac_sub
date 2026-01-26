"""add project task comments

Revision ID: 7f3c2a9e6c51
Revises: 2d4f7d5b3b0a
Create Date: 2026-01-14 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "7f3c2a9e6c51"
down_revision = "2d4f7d5b3b0a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "project_task_comments" not in existing_tables:
        op.create_table(
            "project_task_comments",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("project_tasks.id"), nullable=False),
            sa.Column("author_person_id", UUID(as_uuid=True), sa.ForeignKey("people.id"), nullable=True),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("project_task_comments")
