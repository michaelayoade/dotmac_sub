"""Add REST API configuration and rate limiting fields to OLT devices

Revision ID: 059_add_olt_rest_and_rate_limit
Revises: 058_add_olt_autofind_last_sync_at
Create Date: 2026-04-24

"""

from alembic import op
import sqlalchemy as sa

revision = "059_add_olt_rest_and_rate_limit"
down_revision = "058_add_olt_autofind_last_sync_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_columns = [col["name"] for col in inspector.get_columns("olt_devices")]

    # REST API configuration fields
    if "api_enabled" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "api_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
                comment="Enable REST API communication with this OLT",
            ),
        )

    if "api_url" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "api_url",
                sa.String(512),
                nullable=True,
                comment="Base URL for OLT REST API (e.g., https://olt.example.com/api)",
            ),
        )

    if "api_port" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "api_port",
                sa.Integer(),
                nullable=True,
                server_default="443",
                comment="REST API port (default 443)",
            ),
        )

    if "api_username" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "api_username",
                sa.String(120),
                nullable=True,
                comment="Username for REST API authentication",
            ),
        )

    if "api_password" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "api_password",
                sa.String(512),
                nullable=True,
                comment="Password for REST API authentication (encrypted)",
            ),
        )

    if "api_token" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "api_token",
                sa.String(1024),
                nullable=True,
                comment="Bearer token for REST API authentication (encrypted)",
            ),
        )

    if "api_auth_type" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "api_auth_type",
                sa.String(20),
                nullable=True,
                comment="Authentication type: basic, bearer, or none",
            ),
        )

    # Rate limiting field
    if "rate_limit_ops_per_minute" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "rate_limit_ops_per_minute",
                sa.Integer(),
                nullable=True,
                server_default="10",
                comment="Maximum operations per minute for this OLT (rate limiting)",
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_columns = [col["name"] for col in inspector.get_columns("olt_devices")]

    columns_to_drop = [
        "api_enabled",
        "api_url",
        "api_port",
        "api_username",
        "api_password",
        "api_token",
        "api_auth_type",
        "rate_limit_ops_per_minute",
    ]

    for col in columns_to_drop:
        if col in existing_columns:
            op.drop_column("olt_devices", col)
