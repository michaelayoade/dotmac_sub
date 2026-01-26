"""add auth constraints and indexes

Revision ID: 6ebaf94af561
Revises: 56136b460f1b
Create Date: 2026-01-09 14:54:20.936969

"""

from alembic import op
import sqlalchemy as sa


revision = '6ebaf94af561'
down_revision = '56136b460f1b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_user_credentials_local_requires_username_password",
        "user_credentials",
        "(provider != 'local') OR (username IS NOT NULL AND password_hash IS NOT NULL)",
    )
    op.create_index(
        "ix_user_credentials_local_username_unique",
        "user_credentials",
        ["username"],
        unique=True,
        postgresql_where=sa.text("provider = 'local'"),
        sqlite_where=sa.text("provider = 'local'"),
    )

    op.drop_index("ix_sessions_token_hash", table_name="sessions")
    op.drop_index("ix_sessions_previous_token_hash", table_name="sessions")
    op.create_index("ux_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_index(
        "ux_sessions_previous_token_hash",
        "sessions",
        ["previous_token_hash"],
        unique=True,
    )

    op.create_unique_constraint("uq_api_keys_key_hash", "api_keys", ["key_hash"])

    op.execute(
        """
        UPDATE domain_settings
        SET value_json = to_json(value_text),
            value_text = NULL
        WHERE value_type = 'json'
          AND value_json IS NULL
          AND value_text IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE domain_settings
        SET value_json = 'null'::json
        WHERE value_type = 'json'
          AND value_json IS NULL
          AND value_text IS NULL
        """
    )
    op.execute(
        """
        UPDATE domain_settings
        SET value_text = NULL
        WHERE value_type = 'json'
          AND value_json IS NOT NULL
          AND value_text IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE domain_settings
        SET value_text = value_json::text,
            value_json = NULL
        WHERE value_type != 'json'
          AND value_text IS NULL
          AND value_json IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE domain_settings
        SET value_text = ''
        WHERE value_type != 'json'
          AND value_text IS NULL
          AND value_json IS NULL
        """
    )
    op.execute(
        """
        UPDATE domain_settings
        SET value_json = NULL
        WHERE value_type != 'json'
          AND value_text IS NOT NULL
          AND value_json IS NOT NULL
        """
    )
    op.create_check_constraint(
        "ck_domain_settings_value_alignment",
        "domain_settings",
        "(value_type = 'json' AND value_json IS NOT NULL AND value_text IS NULL) "
        "OR (value_type != 'json' AND value_text IS NOT NULL AND value_json IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_domain_settings_value_alignment", "domain_settings", type_="check"
    )

    op.drop_constraint("uq_api_keys_key_hash", "api_keys", type_="unique")

    op.drop_index("ux_sessions_previous_token_hash", table_name="sessions")
    op.drop_index("ux_sessions_token_hash", table_name="sessions")
    op.create_index(
        "ix_sessions_previous_token_hash",
        "sessions",
        ["previous_token_hash"],
        unique=False,
    )
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=False)

    op.drop_index(
        "ix_user_credentials_local_username_unique", table_name="user_credentials"
    )
    op.drop_constraint(
        "ck_user_credentials_local_requires_username_password",
        "user_credentials",
        type_="check",
    )
