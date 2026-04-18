"""Add wifi_ssid and wifi_password to ont_units.

Stores desired WiFi configuration on the ONT record, enabling automatic
push via TR-069 when the ONT comes online and connects to ACS.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "024_add_wifi_ssid_password_to_ont_units"
down_revision = "023_add_tr069_olt_profile_to_ont_units"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _column_exists(table_name, column.name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _column_exists(table_name, column_name):
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    _add_column_if_missing(
        "ont_units",
        sa.Column("wifi_ssid", sa.String(64), nullable=True),
    )
    _add_column_if_missing(
        "ont_units",
        sa.Column("wifi_password", sa.String(120), nullable=True),
    )


def downgrade() -> None:
    _drop_column_if_exists("ont_units", "wifi_password")
    _drop_column_if_exists("ont_units", "wifi_ssid")
