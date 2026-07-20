"""Index service_extension_entries (subscriber_id, created_at).

``bulk_extension_shield_reasons`` (the dunning-shield lookup added with the
service-extension resume/shield fix) filters this table by subscriber and
entry age on every scheduled dunning act; without an index that degrades to a
full scan as extension entries accumulate.

``IF NOT EXISTS`` because the index was already created in production with
``CREATE INDEX CONCURRENTLY`` ahead of this migration.

Revision ID: 202_index_service_extension_entries
Revises: 173_subscription_bundles
"""

from __future__ import annotations

from alembic import op

revision = "202_index_service_extension_entries"
down_revision = "173_subscription_bundles"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_service_extension_entries_subscriber_created"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "service_extension_entries",
        ["subscriber_id", "created_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="service_extension_entries", if_exists=True)
