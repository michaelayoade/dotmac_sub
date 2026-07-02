"""Add nas_devices.mikrotik_api_port (first-class RouterOS API port).

Replaces the brittle ``mikrotik_api_port:NNNN`` device tag with a real column
for the bandwidth poller / NAS adapter (8728 plaintext / 8729 API-SSL). Nullable;
resolvers fall back to the legacy tag, then 8728.

Revision ID: 199_add_nas_mikrotik_api_port
Revises: 198_add_bandwidth_samples_sample_at_index
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "199_add_nas_mikrotik_api_port"
down_revision = "198_add_bandwidth_samples_sample_at_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "nas_devices", sa.Column("mikrotik_api_port", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("nas_devices", "mikrotik_api_port")
