"""Add wifi_ssid and wifi_password to ont_units.

Stores desired WiFi configuration on the ONT record, enabling automatic
push via TR-069 when the ONT comes online and connects to ACS.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "024_add_wifi_ssid_password_to_ont_units"
down_revision = "023_add_tr069_olt_profile_to_ont_units"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ont_units",
        sa.Column("wifi_ssid", sa.String(64), nullable=True),
    )
    op.add_column(
        "ont_units",
        sa.Column("wifi_password", sa.String(120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ont_units", "wifi_password")
    op.drop_column("ont_units", "wifi_ssid")
