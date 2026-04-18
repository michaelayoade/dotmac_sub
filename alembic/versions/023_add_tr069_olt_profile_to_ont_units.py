"""Add TR-069 OLT profile selection to ONT units.

This stores the manually selected TR-069 OLT profile for an ONT directly on
the ONT record, so it persists even when no service-order execution context
is present.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "023_add_tr069_olt_profile_to_ont_units"
down_revision = "022_next_available_ip"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def upgrade() -> None:
    if not _column_exists("ont_units", "tr069_olt_profile_id"):
        op.add_column(
            "ont_units",
            sa.Column("tr069_olt_profile_id", sa.Integer, nullable=True),
        )


def downgrade() -> None:
    if _column_exists("ont_units", "tr069_olt_profile_id"):
        op.drop_column("ont_units", "tr069_olt_profile_id")
