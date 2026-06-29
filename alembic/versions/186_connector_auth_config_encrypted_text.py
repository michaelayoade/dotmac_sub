"""Store connector auth_config as encrypted TEXT (encrypt-at-rest).

``ConnectorConfig.auth_config`` becomes an ``EncryptedJSON`` column whose blob is
a single ``enc:``/``plain:`` string. Storing it as TEXT (not JSON) keeps the raw
value a plain string on every dialect, so ``credential_key_rotation`` can re-encrypt
it with a new Fernet key via straight SQL. Legacy plaintext rows (a JSON object)
are cast to their JSON text and still decode transparently until the one-off
re-encrypt (scripts/one_off/encrypt_connector_auth_config.py) runs.

Revision ID: 186_connector_auth_config_text
Revises: 185_router_rest_api_username_width
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "186_connector_auth_config_text"
down_revision = "185_router_rest_api_username_width"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "connector_configs",
        "auth_config",
        existing_type=sa.JSON(),
        type_=sa.Text(),
        existing_nullable=True,
        postgresql_using="auth_config::text",
    )


def downgrade() -> None:
    op.alter_column(
        "connector_configs",
        "auth_config",
        existing_type=sa.Text(),
        type_=sa.JSON(),
        existing_nullable=True,
        # Wrap any text (incl. encrypted blobs) as a valid JSON string value.
        postgresql_using="to_jsonb(auth_config)",
    )
