"""Reseller service requests: new-connection / installation queue.

Revision ID: 134_add_reseller_service_requests
Revises: 133_add_refresh_attempted_at
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "134_add_reseller_service_requests"
down_revision = "133_add_refresh_attempted_at"
branch_labels = None
depends_on = None

_TABLE = "reseller_service_requests"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _TABLE in inspect(bind).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "reseller_id",
            UUID(as_uuid=True),
            sa.ForeignKey("resellers.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=True,
        ),
        sa.Column("contact_name", sa.String(160)),
        sa.Column("contact_phone", sa.String(40)),
        sa.Column("contact_email", sa.String(255)),
        sa.Column("address", sa.Text),
        sa.Column("latitude", sa.Float),
        sa.Column("longitude", sa.Float),
        sa.Column(
            "serviceability",
            sa.Enum("unknown", "serviceable", "not_serviceable", name="serviceability"),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "new",
                "reviewing",
                "scheduled",
                "completed",
                "rejected",
                name="servicerequeststatus",
            ),
            nullable=False,
            server_default="new",
            index=True,
        ),
        sa.Column("notes", sa.Text),
        sa.Column("admin_notes", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_table(_TABLE)
    sa.Enum(name="serviceability").drop(bind, checkfirst=True)
    sa.Enum(name="servicerequeststatus").drop(bind, checkfirst=True)
