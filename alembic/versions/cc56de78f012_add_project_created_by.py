"""Add created_by_person_id to projects.

Revision ID: cc56de78f012
Revises: bb34cd56ef78
Create Date: 2026-01-14 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "cc56de78f012"
down_revision = "bb34cd56ef78"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("projects")}
    if "created_by_person_id" not in columns:
        op.add_column(
            "projects",
            sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    fks = {fk["name"] for fk in inspector.get_foreign_keys("projects")}
    if "fk_projects_created_by_person_id_people" not in fks:
        op.create_foreign_key(
            "fk_projects_created_by_person_id_people",
            "projects",
            "people",
            ["created_by_person_id"],
            ["id"],
        )


def downgrade() -> None:
    op.drop_constraint("fk_projects_created_by_person_id_people", "projects", type_="foreignkey")
    op.drop_column("projects", "created_by_person_id")
