"""Add owner subscriber id to stored files.

Revision ID: c9d0e1f2a3b5
Revises: b8c9d0e1f2a3
Create Date: 2026-03-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "c9d0e1f2a3b5"
down_revision: str | Sequence[str] | None = "b8c9d0e1f2a3"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("stored_files")}
    indexes = {index["name"] for index in inspector.get_indexes("stored_files")}
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("stored_files")}

    if "owner_subscriber_id" not in columns:
        op.add_column(
            "stored_files",
            sa.Column("owner_subscriber_id", UUID(as_uuid=True), nullable=True),
        )

    if "ix_stored_files_owner_active" not in indexes:
        op.create_index(
            "ix_stored_files_owner_active",
            "stored_files",
            ["owner_subscriber_id", "is_deleted"],
        )

    if "fk_stored_files_owner_subscriber_id_subscribers" not in foreign_keys:
        op.create_foreign_key(
            "fk_stored_files_owner_subscriber_id_subscribers",
            "stored_files",
            "subscribers",
            ["owner_subscriber_id"],
            ["id"],
        )

    bind.execute(
        text(
            """
            UPDATE stored_files sf
            SET owner_subscriber_id = s.id
            FROM subscribers s
            WHERE sf.owner_subscriber_id IS NULL
              AND sf.organization_id IS NOT NULL
              AND s.organization_id = sf.organization_id
              AND lower(COALESCE(s.metadata->>'subscriber_category', '')) = 'business'
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("stored_files")}
    indexes = {index["name"] for index in inspector.get_indexes("stored_files")}
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("stored_files")}

    if "fk_stored_files_owner_subscriber_id_subscribers" in foreign_keys:
        op.drop_constraint(
            "fk_stored_files_owner_subscriber_id_subscribers",
            "stored_files",
            type_="foreignkey",
        )
    if "ix_stored_files_owner_active" in indexes:
        op.drop_index("ix_stored_files_owner_active", table_name="stored_files")
    if "owner_subscriber_id" in columns:
        op.drop_column("stored_files", "owner_subscriber_id")
