"""Add organization primary login subscriber reference.

Revision ID: z7b8c9d0e1f2
Revises: z6a7b8c9d0e1
Create Date: 2026-02-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "z7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "z6a7b8c9d0e1"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("organizations")}
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("organizations")}

    if "primary_login_subscriber_id" not in columns:
        op.add_column(
            "organizations",
            sa.Column("primary_login_subscriber_id", UUID(as_uuid=True), nullable=True),
        )

    if "fk_organizations_primary_login_subscriber_id" not in foreign_keys:
        op.create_foreign_key(
            "fk_organizations_primary_login_subscriber_id",
            "organizations",
            "subscribers",
            ["primary_login_subscriber_id"],
            ["id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("organizations")}
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("organizations")}

    if "fk_organizations_primary_login_subscriber_id" in foreign_keys:
        op.drop_constraint(
            "fk_organizations_primary_login_subscriber_id",
            "organizations",
            type_="foreignkey",
        )
    if "primary_login_subscriber_id" in columns:
        op.drop_column("organizations", "primary_login_subscriber_id")
