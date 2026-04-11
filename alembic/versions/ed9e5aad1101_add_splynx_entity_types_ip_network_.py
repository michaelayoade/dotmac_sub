"""add_splynx_entity_types_ip_network_radius_profile

Revision ID: ed9e5aad1101
Revises: 013_add_ont_contact_column
Create Date: 2026-04-11 13:30:31.486412

"""

from alembic import op


revision = 'ed9e5aad1101'
down_revision = '013_add_ont_contact_column'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new values to the splynxentitytype enum
    # PostgreSQL requires ALTER TYPE ... ADD VALUE for each new value
    # Note: ADD VALUE cannot be run inside a transaction block, so we commit each separately
    op.execute("ALTER TYPE splynxentitytype ADD VALUE IF NOT EXISTS 'ip_network'")
    op.execute("ALTER TYPE splynxentitytype ADD VALUE IF NOT EXISTS 'radius_profile'")


def downgrade() -> None:
    # PostgreSQL does not support removing values from enums
    # The values will remain but won't be used if migration is rolled back
    pass
