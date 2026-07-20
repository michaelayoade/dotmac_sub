"""Add timestamp index for bandwidth report windows.

Revision ID: 198_add_bandwidth_samples_sample_at_index
Revises: 196_merge_role_scope_and_main_heads
Create Date: 2026-07-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "198_add_bandwidth_samples_sample_at_index"
down_revision = "196_merge_role_scope_and_main_heads"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_bandwidth_samples_sample_at"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {index["name"] for index in inspector.get_indexes("bandwidth_samples")}
    if _INDEX_NAME not in existing:
        op.create_index(_INDEX_NAME, "bandwidth_samples", ["sample_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {index["name"] for index in inspector.get_indexes("bandwidth_samples")}
    if _INDEX_NAME in existing:
        op.drop_index(_INDEX_NAME, table_name="bandwidth_samples")
