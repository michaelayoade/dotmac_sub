"""Add project_type to service orders."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM

# revision identifiers, used by Alembic.
revision = "a9b8c7d6e5f4"
down_revision = "f3b7c9d1a2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("service_orders")}
    if "project_type" not in columns:
        op.add_column(
            "service_orders",
            sa.Column(
                "project_type",
                ENUM(
                    "cable_rerun",
                    "fiber_optics_relocation",
                    "radio_fiber_relocation",
                    "fiber_optics_installation",
                    "radio_installation",
                    name="projecttype",
                    create_type=False,
                ),
                nullable=True,
            ),
        )


def downgrade() -> None:
    op.drop_column("service_orders", "project_type")
