"""Add management IP pool to ONT provisioning profiles.

Allows operators to select a static IP from a predefined pool when configuring
management IP on ONTs, instead of manually entering IP addresses.

Revision ID: 021_add_mgmt_ip_pool_to_provisioning_profile
Revises: 020_add_ont_lan_configuration_fields
Create Date: 2026-04-15
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision = "021_add_mgmt_ip_pool_to_provisioning_profile"
down_revision = "020_add_ont_lan_configuration_fields"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def _index_exists(table_name: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def _foreign_key_exists(table_name: str, constraint_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return constraint_name in {
        fk["name"] for fk in inspector.get_foreign_keys(table_name)
    }


def upgrade() -> None:
    # Add mgmt_ip_pool_id column to ont_provisioning_profiles
    if not _column_exists("ont_provisioning_profiles", "mgmt_ip_pool_id"):
        op.add_column(
            "ont_provisioning_profiles",
            sa.Column("mgmt_ip_pool_id", UUID(as_uuid=True), nullable=True),
        )
    # Add foreign key constraint
    if not _foreign_key_exists(
        "ont_provisioning_profiles", "fk_ont_prov_profiles_mgmt_ip_pool"
    ):
        op.create_foreign_key(
            "fk_ont_prov_profiles_mgmt_ip_pool",
            "ont_provisioning_profiles",
            "ip_pools",
            ["mgmt_ip_pool_id"],
            ["id"],
            ondelete="SET NULL",
        )
    # Add index for faster lookups
    if not _index_exists(
        "ont_provisioning_profiles", "ix_ont_provisioning_profiles_mgmt_ip_pool_id"
    ):
        op.create_index(
            "ix_ont_provisioning_profiles_mgmt_ip_pool_id",
            "ont_provisioning_profiles",
            ["mgmt_ip_pool_id"],
        )


def downgrade() -> None:
    if _index_exists(
        "ont_provisioning_profiles", "ix_ont_provisioning_profiles_mgmt_ip_pool_id"
    ):
        op.drop_index(
            "ix_ont_provisioning_profiles_mgmt_ip_pool_id",
            table_name="ont_provisioning_profiles",
        )
    if _foreign_key_exists(
        "ont_provisioning_profiles", "fk_ont_prov_profiles_mgmt_ip_pool"
    ):
        op.drop_constraint(
            "fk_ont_prov_profiles_mgmt_ip_pool",
            "ont_provisioning_profiles",
            type_="foreignkey",
        )
    if _column_exists("ont_provisioning_profiles", "mgmt_ip_pool_id"):
        op.drop_column("ont_provisioning_profiles", "mgmt_ip_pool_id")
