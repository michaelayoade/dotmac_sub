"""Add project comments table.

Revision ID: bb34cd56ef78
Revises: ab12cd34ef56
Create Date: 2026-01-14 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "bb34cd56ef78"
down_revision = "ab12cd34ef56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "project_comments" not in existing_tables:
        op.create_table(
            "project_comments",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("author_person_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("attachments", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
            sa.ForeignKeyConstraint(["author_person_id"], ["people.id"]),
        )


def downgrade() -> None:
    op.drop_table("project_comments")
