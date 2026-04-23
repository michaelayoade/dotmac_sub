"""Add hardware_version and software_version to ont_units.

Revision ID: 055_ont_versions
Revises: 054_add_olt_default_provisioning_profile
Create Date: 2026-04-23

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "055_ont_versions"
down_revision = "054_add_olt_default_prov_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add hardware_version column if not exists
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("ont_units")]

    if "hardware_version" not in columns:
        op.add_column(
            "ont_units",
            sa.Column("hardware_version", sa.String(120), nullable=True),
        )

    if "software_version" not in columns:
        op.add_column(
            "ont_units",
            sa.Column("software_version", sa.String(120), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("ont_units", "software_version")
    op.drop_column("ont_units", "hardware_version")
