"""Add management IP pool to ONT provisioning profiles.

Allows operators to select a static IP from a predefined pool when configuring
management IP on ONTs, instead of manually entering IP addresses.

Revision ID: 021_add_mgmt_ip_pool_to_provisioning_profile
Revises: 020_add_ont_lan_configuration_fields
Create Date: 2026-04-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "021_add_mgmt_ip_pool_to_provisioning_profile"
down_revision = "020_add_ont_lan_configuration_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add mgmt_ip_pool_id column to ont_provisioning_profiles
    op.add_column(
        "ont_provisioning_profiles",
        sa.Column("mgmt_ip_pool_id", UUID(as_uuid=True), nullable=True),
    )
    # Add foreign key constraint
    op.create_foreign_key(
        "fk_ont_prov_profiles_mgmt_ip_pool",
        "ont_provisioning_profiles",
        "ip_pools",
        ["mgmt_ip_pool_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Add index for faster lookups
    op.create_index(
        "ix_ont_provisioning_profiles_mgmt_ip_pool_id",
        "ont_provisioning_profiles",
        ["mgmt_ip_pool_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ont_provisioning_profiles_mgmt_ip_pool_id",
        table_name="ont_provisioning_profiles",
    )
    op.drop_constraint(
        "fk_ont_prov_profiles_mgmt_ip_pool",
        "ont_provisioning_profiles",
        type_="foreignkey",
    )
    op.drop_column("ont_provisioning_profiles", "mgmt_ip_pool_id")
