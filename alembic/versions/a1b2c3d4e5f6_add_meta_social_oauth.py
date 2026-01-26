"""Add Meta (Facebook/Instagram) social OAuth support.

Creates the oauth_tokens table for storing OAuth access tokens for Meta and other
OAuth providers. Also adds new enum values for Facebook/Instagram channel and
connector types.

Revision ID: a1b2c3d4e5f6
Revises: f3b7c9d1a2e4
Create Date: 2026-01-15

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "f3b7c9d1a2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new values to channeltype enum
    op.execute("ALTER TYPE channeltype ADD VALUE IF NOT EXISTS 'facebook_messenger'")
    op.execute("ALTER TYPE channeltype ADD VALUE IF NOT EXISTS 'instagram_dm'")

    # Add new values to connectortype enum
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'facebook'")
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'instagram'")

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # Create oauth_tokens table
    if "oauth_tokens" not in existing_tables:
        op.create_table(
            "oauth_tokens",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "connector_config_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("connector_configs.id"),
                nullable=False,
            ),
            # Provider identification
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("account_type", sa.String(64), nullable=False),
            sa.Column("external_account_id", sa.String(120), nullable=False),
            sa.Column("external_account_name", sa.String(255), nullable=True),
            # Token storage
            sa.Column("access_token", sa.Text(), nullable=True),
            sa.Column("refresh_token", sa.Text(), nullable=True),
            sa.Column("token_type", sa.String(64), nullable=True),
            sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
            # Token metadata
            sa.Column("scopes", postgresql.JSON(), nullable=True),
            sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("refresh_error", sa.Text(), nullable=True),
            # Status
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("metadata", postgresql.JSON(), nullable=True),
            # Timestamps
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    # Refresh state for constraints/indexes
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # Create unique constraint for one token per account per connector
    if "oauth_tokens" in existing_tables:
        existing_constraints = {c["name"] for c in inspector.get_unique_constraints("oauth_tokens")}
        if "uq_oauth_tokens_connector_provider_account" not in existing_constraints:
            op.create_unique_constraint(
                "uq_oauth_tokens_connector_provider_account",
                "oauth_tokens",
                ["connector_config_id", "provider", "external_account_id"],
            )

        # Create indexes for common queries
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("oauth_tokens")}
        if "ix_oauth_tokens_connector_config_id" not in existing_indexes:
            op.create_index(
                "ix_oauth_tokens_connector_config_id",
                "oauth_tokens",
                ["connector_config_id"],
            )
        if "ix_oauth_tokens_token_expires_at" not in existing_indexes:
            op.create_index(
                "ix_oauth_tokens_token_expires_at",
                "oauth_tokens",
                ["token_expires_at"],
            )
        if "ix_oauth_tokens_provider" not in existing_indexes:
            op.create_index(
                "ix_oauth_tokens_provider",
                "oauth_tokens",
                ["provider"],
            )


def downgrade() -> None:
    # Drop indexes
    op.drop_index("ix_oauth_tokens_provider", "oauth_tokens")
    op.drop_index("ix_oauth_tokens_token_expires_at", "oauth_tokens")
    op.drop_index("ix_oauth_tokens_connector_config_id", "oauth_tokens")

    # Drop unique constraint
    op.drop_constraint(
        "uq_oauth_tokens_connector_provider_account", "oauth_tokens", type_="unique"
    )

    # Drop table
    op.drop_table("oauth_tokens")

    # Note: PostgreSQL doesn't support removing enum values easily
    # The channeltype and connectortype enum values will remain
    # but will be unused if this migration is downgraded
