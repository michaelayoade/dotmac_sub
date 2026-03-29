"""Add service_order_id to projects."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "b1c2d3e4f5a6"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("projects")}
    if "service_order_id" not in columns:
        op.add_column(
            "projects",
            sa.Column("service_order_id", UUID(as_uuid=True), nullable=True),
        )
    project_fks = {fk["name"] for fk in inspector.get_foreign_keys("projects")}
    if "fk_projects_service_order_id" not in project_fks:
        op.create_foreign_key(
            "fk_projects_service_order_id",
            "projects",
            "service_orders",
            ["service_order_id"],
            ["id"],
        )
    project_uniques = {c["name"] for c in inspector.get_unique_constraints("projects")}
    if "uq_projects_service_order_id" not in project_uniques:
        op.create_unique_constraint(
            "uq_projects_service_order_id",
            "projects",
            ["service_order_id"],
        )


def downgrade() -> None:
    op.drop_constraint(
        "uq_projects_service_order_id",
        "projects",
        type_="unique",
    )
    op.drop_constraint(
        "fk_projects_service_order_id",
        "projects",
        type_="foreignkey",
    )
    op.drop_column("projects", "service_order_id")
