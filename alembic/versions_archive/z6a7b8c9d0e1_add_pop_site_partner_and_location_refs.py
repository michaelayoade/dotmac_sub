"""Add POP site partner and location references.

Revision ID: z6a7b8c9d0e1
Revises: e5f6a7b8c9d0, f6a7b8c9d0e1, y5z6a7b8c9d0
Create Date: 2026-02-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "z6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = (
    "e5f6a7b8c9d0",
    "f6a7b8c9d0e1",
    "y5z6a7b8c9d0",
)
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("pop_sites")}
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("pop_sites")}

    if "organization_id" not in columns:
        op.add_column("pop_sites", sa.Column("organization_id", UUID(as_uuid=True), nullable=True))
    if "reseller_id" not in columns:
        op.add_column("pop_sites", sa.Column("reseller_id", UUID(as_uuid=True), nullable=True))

    if "fk_pop_sites_organization_id" not in foreign_keys:
        op.create_foreign_key(
            "fk_pop_sites_organization_id",
            "pop_sites",
            "organizations",
            ["organization_id"],
            ["id"],
        )
    if "fk_pop_sites_reseller_id" not in foreign_keys:
        op.create_foreign_key(
            "fk_pop_sites_reseller_id",
            "pop_sites",
            "resellers",
            ["reseller_id"],
            ["id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("pop_sites")}
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("pop_sites")}

    if "fk_pop_sites_reseller_id" in foreign_keys:
        op.drop_constraint("fk_pop_sites_reseller_id", "pop_sites", type_="foreignkey")
    if "fk_pop_sites_organization_id" in foreign_keys:
        op.drop_constraint("fk_pop_sites_organization_id", "pop_sites", type_="foreignkey")

    if "reseller_id" in columns:
        op.drop_column("pop_sites", "reseller_id")
    if "organization_id" in columns:
        op.drop_column("pop_sites", "organization_id")
