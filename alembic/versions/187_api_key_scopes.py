"""Add scopes to API keys.

``api_keys.scopes`` is a JSON array of permission keys the key may exercise
(wildcard-aware, like role grants). Empty = fail-closed: the key authenticates
identity but carries no access.

Revision ID: 187_api_key_scopes
Revises: 186_connector_auth_config_text
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "187_api_key_scopes"
down_revision = "186_connector_auth_config_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column(
            "scopes",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "scopes")
