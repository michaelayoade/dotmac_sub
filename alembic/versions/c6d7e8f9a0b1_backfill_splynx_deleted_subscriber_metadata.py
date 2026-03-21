"""Backfill Splynx deleted subscriber metadata.

Revision ID: c6d7e8f9a0b1
Revises: b1c2d3e4f5g6
Create Date: 2026-03-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c6d7e8f9a0b1"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5g6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
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
    op.execute(
        """
        UPDATE subscribers
        SET metadata = (metadata::jsonb - 'splynx_deleted')::json
        WHERE splynx_customer_id IS NOT NULL
          AND COALESCE(metadata ->> 'splynx_deleted', '') IN ('true', '1', 'yes', 'on');
        """
    )
