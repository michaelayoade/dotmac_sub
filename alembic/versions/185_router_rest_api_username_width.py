"""Widen encrypted router REST API username storage.

Revision ID: 185_router_rest_api_username_width
Revises: 184_session_device_id
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "185_router_rest_api_username_width"
down_revision = "184_session_device_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "routers",
        "rest_api_username",
        existing_type=sa.String(length=255),
        type_=sa.String(length=512),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "routers",
        "rest_api_username",
        existing_type=sa.String(length=512),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
