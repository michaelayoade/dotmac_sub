"""Backfill Splynx deleted subscriber metadata.

Revision ID: c6d7e8f9a0b1
Revises: b1c2d3e4f5g6
Create Date: 2026-03-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c6d7e8f9a0b1"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5g6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _column_exists(bind, table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if table_name not in tables:
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    # Skip on fresh DBs where required columns don't exist yet
    bind = op.get_bind()
    if not _column_exists(bind, "subscribers", "splynx_customer_id"):
        return
    if not _column_exists(bind, "subscribers", "metadata"):
        return
    if not _column_exists(bind, "subscribers", "status"):
        return

    op.execute(
        """
        UPDATE subscribers
        SET metadata = jsonb_set(
            COALESCE(metadata::jsonb, '{}'::jsonb),
            '{splynx_deleted}',
            'true'::jsonb,
            true
        )::json
        WHERE splynx_customer_id IS NOT NULL
          AND is_active IS FALSE
          AND status = 'canceled'
          AND COALESCE(metadata ->> 'splynx_status', '') NOT IN ('', 'deleted', 'canceled')
          AND COALESCE(metadata ->> 'splynx_deleted', '') NOT IN ('true', '1', 'yes', 'on');
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "subscribers", "splynx_customer_id"):
        return

    op.execute(
        """
        UPDATE subscribers
        SET metadata = (metadata::jsonb - 'splynx_deleted')::json
        WHERE splynx_customer_id IS NOT NULL
          AND COALESCE(metadata ->> 'splynx_deleted', '') IN ('true', '1', 'yes', 'on');
        """
    )
