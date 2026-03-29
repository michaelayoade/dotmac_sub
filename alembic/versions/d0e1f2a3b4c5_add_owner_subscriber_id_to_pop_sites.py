"""Add owner subscriber id to pop sites.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b5
Create Date: 2026-03-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | Sequence[str] | None = "c9d0e1f2a3b5"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("pop_sites")}
    indexes = {index["name"] for index in inspector.get_indexes("pop_sites")}
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("pop_sites")}

    if "owner_subscriber_id" not in columns:
        op.add_column(
            "pop_sites",
            sa.Column("owner_subscriber_id", UUID(as_uuid=True), nullable=True),
        )

    if "ix_pop_sites_owner_subscriber_id" not in indexes:
        op.create_index(
            "ix_pop_sites_owner_subscriber_id",
            "pop_sites",
            ["owner_subscriber_id"],
        )

    if "fk_pop_sites_owner_subscriber_id_subscribers" not in foreign_keys:
        op.create_foreign_key(
            "fk_pop_sites_owner_subscriber_id_subscribers",
            "pop_sites",
            "subscribers",
            ["owner_subscriber_id"],
            ["id"],
        )

    # Skip data backfill on fresh DBs where metadata column doesn't exist yet
    sub_cols = {c["name"] for c in inspector.get_columns("subscribers")}
    if "metadata" not in sub_cols or "organization_id" not in sub_cols:
        return

    bind.execute(
        text(
            """
            UPDATE pop_sites ps
            SET owner_subscriber_id = s.id
            FROM subscribers s
            WHERE ps.owner_subscriber_id IS NULL
              AND ps.organization_id IS NOT NULL
              AND s.organization_id = ps.organization_id
              AND lower(COALESCE(s.metadata->>'subscriber_category', '')) = 'business'
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("pop_sites")}
    indexes = {index["name"] for index in inspector.get_indexes("pop_sites")}
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("pop_sites")}

    if "fk_pop_sites_owner_subscriber_id_subscribers" in foreign_keys:
        op.drop_constraint(
            "fk_pop_sites_owner_subscriber_id_subscribers",
            "pop_sites",
            type_="foreignkey",
        )
    if "ix_pop_sites_owner_subscriber_id" in indexes:
        op.drop_index("ix_pop_sites_owner_subscriber_id", table_name="pop_sites")
    if "owner_subscriber_id" in columns:
        op.drop_column("pop_sites", "owner_subscriber_id")
