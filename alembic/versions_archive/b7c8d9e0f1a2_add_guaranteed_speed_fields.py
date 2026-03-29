"""Add guaranteed speed fields to catalog offers.

Revision ID: b7c8d9e0f1a2
Revises: f4a1b2c3d5e6
Create Date: 2026-01-19 15:30:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b7c8d9e0f1a2"
down_revision = "f4a1b2c3d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("catalog_offers")}

    guaranteed_speed_type = postgresql.ENUM(
        "none",
        "relative",
        "fixed",
        name="guaranteedspeedtype",
    )
    guaranteed_speed_type.create(bind, checkfirst=True)

    if "guaranteed_speed_limit_at" not in columns:
        op.add_column(
            "catalog_offers",
            sa.Column("guaranteed_speed_limit_at", sa.Integer(), nullable=True),
        )
    if "guaranteed_speed" not in columns:
        op.add_column(
            "catalog_offers",
            sa.Column(
                "guaranteed_speed",
                guaranteed_speed_type,
                server_default="none",
                nullable=False,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("catalog_offers")}

    if "guaranteed_speed" in columns:
        op.drop_column("catalog_offers", "guaranteed_speed")
    if "guaranteed_speed_limit_at" in columns:
        op.drop_column("catalog_offers", "guaranteed_speed_limit_at")

    guaranteed_speed_type = postgresql.ENUM(
        "none",
        "relative",
        "fixed",
        name="guaranteedspeedtype",
    )
    guaranteed_speed_type.drop(bind, checkfirst=True)
