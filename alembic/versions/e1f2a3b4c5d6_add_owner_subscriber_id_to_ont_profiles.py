"""Add owner subscriber id to ONT provisioning profiles.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-03-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d0e1f2a3b4c5"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {
        column["name"] for column in inspector.get_columns("ont_provisioning_profiles")
    }
    foreign_keys = {
        fk["name"] for fk in inspector.get_foreign_keys("ont_provisioning_profiles")
    }
    unique_constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("ont_provisioning_profiles")
    }

    if "owner_subscriber_id" not in columns:
        op.add_column(
            "ont_provisioning_profiles",
            sa.Column("owner_subscriber_id", UUID(as_uuid=True), nullable=True),
        )

    if "fk_ont_prov_profiles_owner_subscriber_id" not in foreign_keys:
        op.create_foreign_key(
            "fk_ont_prov_profiles_owner_subscriber_id",
            "ont_provisioning_profiles",
            "subscribers",
            ["owner_subscriber_id"],
            ["id"],
        )

    bind.execute(
        text(
            """
            UPDATE ont_provisioning_profiles opp
            SET owner_subscriber_id = s.id
            FROM subscribers s
            WHERE opp.owner_subscriber_id IS NULL
              AND opp.organization_id IS NOT NULL
              AND s.organization_id = opp.organization_id
              AND lower(COALESCE(s.metadata->>'subscriber_category', '')) = 'business'
            """
        )
    )

    if "uq_ont_prov_profiles_org_name" in unique_constraints:
        op.drop_constraint(
            "uq_ont_prov_profiles_org_name",
            "ont_provisioning_profiles",
            type_="unique",
        )

    inspector = inspect(bind)
    unique_constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("ont_provisioning_profiles")
    }
    if "uq_ont_prov_profiles_owner_name" not in unique_constraints:
        op.create_unique_constraint(
            "uq_ont_prov_profiles_owner_name",
            "ont_provisioning_profiles",
            ["owner_subscriber_id", "name"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {
        column["name"] for column in inspector.get_columns("ont_provisioning_profiles")
    }
    foreign_keys = {
        fk["name"] for fk in inspector.get_foreign_keys("ont_provisioning_profiles")
    }
    unique_constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("ont_provisioning_profiles")
    }

    if "uq_ont_prov_profiles_owner_name" in unique_constraints:
        op.drop_constraint(
            "uq_ont_prov_profiles_owner_name",
            "ont_provisioning_profiles",
            type_="unique",
        )
    if "uq_ont_prov_profiles_org_name" not in unique_constraints:
        op.create_unique_constraint(
            "uq_ont_prov_profiles_org_name",
            "ont_provisioning_profiles",
            ["organization_id", "name"],
        )
    if "fk_ont_prov_profiles_owner_subscriber_id" in foreign_keys:
        op.drop_constraint(
            "fk_ont_prov_profiles_owner_subscriber_id",
            "ont_provisioning_profiles",
            type_="foreignkey",
        )
    if "owner_subscriber_id" in columns:
        op.drop_column("ont_provisioning_profiles", "owner_subscriber_id")
