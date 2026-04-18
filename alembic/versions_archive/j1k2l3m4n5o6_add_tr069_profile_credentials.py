"""add_tr069_profile_credentials

Revision ID: j1k2l3m4n5o6
Revises: h1a2b3c4d5e6
Create Date: 2026-03-09 16:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "j1k2l3m4n5o6"
down_revision = "h1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("tr069_acs_servers")}

    if "cwmp_url" not in existing_columns:
        op.add_column("tr069_acs_servers", sa.Column("cwmp_url", sa.String(length=255), nullable=True))
    if "cwmp_username" not in existing_columns:
        op.add_column("tr069_acs_servers", sa.Column("cwmp_username", sa.String(length=120), nullable=True))
    if "cwmp_password" not in existing_columns:
        op.add_column("tr069_acs_servers", sa.Column("cwmp_password", sa.String(length=255), nullable=True))
    if "connection_request_username" not in existing_columns:
        op.add_column(
            "tr069_acs_servers",
            sa.Column("connection_request_username", sa.String(length=120), nullable=True),
        )
    if "connection_request_password" not in existing_columns:
        op.add_column(
            "tr069_acs_servers",
            sa.Column("connection_request_password", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("tr069_acs_servers")}

    if "connection_request_password" in existing_columns:
        op.drop_column("tr069_acs_servers", "connection_request_password")
    if "connection_request_username" in existing_columns:
        op.drop_column("tr069_acs_servers", "connection_request_username")
    if "cwmp_password" in existing_columns:
        op.drop_column("tr069_acs_servers", "cwmp_password")
    if "cwmp_username" in existing_columns:
        op.drop_column("tr069_acs_servers", "cwmp_username")
    if "cwmp_url" in existing_columns:
        op.drop_column("tr069_acs_servers", "cwmp_url")
