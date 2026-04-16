"""Add TR-069 OLT profile selection to ONT units.

This stores the manually selected TR-069 OLT profile for an ONT directly on
the ONT record, so it persists even when no service-order execution context
is present.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "023_add_tr069_olt_profile_to_ont_units"
down_revision = "022_next_available_ip"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ont_units",
        sa.Column("tr069_olt_profile_id", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ont_units", "tr069_olt_profile_id")
