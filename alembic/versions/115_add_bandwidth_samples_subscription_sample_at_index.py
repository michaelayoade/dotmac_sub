"""Add subscription/sample timestamp lookup index for bandwidth samples.

Revision ID: 115_add_bandwidth_samples_subscription_sample_at_index
Revises: 114_add_subscription_access_state
Create Date: 2026-05-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "115_add_bandwidth_samples_subscription_sample_at_index"
down_revision = "114_add_subscription_access_state"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_bandwidth_samples_subscription_sample_at"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {index["name"] for index in inspector.get_indexes("bandwidth_samples")}
    if _INDEX_NAME not in existing:
        op.create_index(
            _INDEX_NAME,
            "bandwidth_samples",
            ["subscription_id", "sample_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {index["name"] for index in inspector.get_indexes("bandwidth_samples")}
    if _INDEX_NAME in existing:
        op.drop_index(_INDEX_NAME, table_name="bandwidth_samples")
