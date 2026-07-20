"""add_splynx_entity_types_ip_network_radius_profile

Revision ID: ed9e5aad1101
Revises: 013_add_ont_contact_column
Create Date: 2026-04-11 13:30:31.486412

"""

import sqlalchemy as sa

from alembic import op

revision = "ed9e5aad1101"
down_revision = "013_add_ont_contact_column"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new values to the splynxentitytype enum.
    # The Splynx models were retired (revision 330), so on a fresh database
    # built from the current models (001_squashed runs create_all) the enum no
    # longer exists — skip the ALTER TYPE rather than fail. Databases created
    # before the retirement still carry the type and receive the new values.
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not bind.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'splynxentitytype'")
    ).scalar():
        return
    op.execute("ALTER TYPE splynxentitytype ADD VALUE IF NOT EXISTS 'ip_network'")
    op.execute("ALTER TYPE splynxentitytype ADD VALUE IF NOT EXISTS 'radius_profile'")


def downgrade() -> None:
    # PostgreSQL does not support removing values from enums
    # The values will remain but won't be used if migration is rolled back
    pass
