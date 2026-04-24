"""Drop server_default from periodic_inform_interval column.

The Python default (from settings.tr069_periodic_inform_interval) is always
used via SQLAlchemy, so the SQL server_default is redundant and causes
inconsistency when the env var differs from the hardcoded "300".

Revision ID: 056_drop_inform_server_default
Revises: 055_ont_versions
Create Date: 2026-04-24

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "056_drop_inform_server_default"
down_revision = "055_ont_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove the server_default from periodic_inform_interval
    # The Python default in the model will always be used via SQLAlchemy
    op.alter_column(
        "tr069_acs_servers",
        "periodic_inform_interval",
        server_default=None,
        existing_type=sa.Integer(),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Restore the server_default
    op.alter_column(
        "tr069_acs_servers",
        "periodic_inform_interval",
        server_default="300",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
