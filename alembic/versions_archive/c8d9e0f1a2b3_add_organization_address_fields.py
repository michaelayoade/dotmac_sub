"""Add address fields to organizations table.

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-01-20

Adds address fields to organizations table to match the customer wizard form.
"""

from alembic import op
import sqlalchemy as sa


revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("organizations")}

    if "address_line1" not in columns:
        op.add_column("organizations", sa.Column("address_line1", sa.String(120), nullable=True))
    if "address_line2" not in columns:
        op.add_column("organizations", sa.Column("address_line2", sa.String(120), nullable=True))
    if "city" not in columns:
        op.add_column("organizations", sa.Column("city", sa.String(80), nullable=True))
    if "region" not in columns:
        op.add_column("organizations", sa.Column("region", sa.String(80), nullable=True))
    if "postal_code" not in columns:
        op.add_column("organizations", sa.Column("postal_code", sa.String(20), nullable=True))
    if "country_code" not in columns:
        op.add_column("organizations", sa.Column("country_code", sa.String(2), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "country_code")
    op.drop_column("organizations", "postal_code")
    op.drop_column("organizations", "region")
    op.drop_column("organizations", "city")
    op.drop_column("organizations", "address_line2")
    op.drop_column("organizations", "address_line1")
