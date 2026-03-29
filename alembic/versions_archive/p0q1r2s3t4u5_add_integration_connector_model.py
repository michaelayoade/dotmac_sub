"""Add integration connectors table.

Revision ID: p0q1r2s3t4u5
Revises: o9p0q1r2s3t4
Create Date: 2026-02-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import ENUM, UUID

from alembic import op

revision: str = "p0q1r2s3t4u5"
down_revision: str | Sequence[str] | None = "o9p0q1r2s3t4"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "integration_connectors" in tables:
        return

    # Create enum types separately with checkfirst
    connector_type_enum = sa.Enum(
        "payment", "accounting", "messaging", "network", "crm", "voice", "custom",
        name="integrationconnectortype",
    )
    status_type_enum = sa.Enum(
        "enabled", "disabled", "not_installed",
        name="integrationconnectorstatus",
    )
    connector_type_enum.create(bind, checkfirst=True)
    status_type_enum.create(bind, checkfirst=True)

    # Use postgresql ENUM with create_type=False so table DDL doesn't re-create them
    ct_col = ENUM(
        "payment", "accounting", "messaging", "network", "crm", "voice", "custom",
        name="integrationconnectortype", create_type=False,
    )
    st_col = ENUM(
        "enabled", "disabled", "not_installed",
        name="integrationconnectorstatus", create_type=False,
    )

    op.create_table(
        "integration_connectors",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("connector_type", ct_col, nullable=False),
        sa.Column("status", st_col, nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "integration_connectors" in tables:
        op.drop_table("integration_connectors")

    connector_type = sa.Enum(name="integrationconnectortype")
    status_type = sa.Enum(name="integrationconnectorstatus")
    connector_type.drop(bind, checkfirst=True)
    status_type.drop(bind, checkfirst=True)
