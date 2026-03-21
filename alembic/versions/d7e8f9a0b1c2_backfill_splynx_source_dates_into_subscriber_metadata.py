"""Backfill Splynx source dates into subscriber metadata.

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-03-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d7e8f9a0b1c2"
down_revision: str | Sequence[str] | None = "c6d7e8f9a0b1"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.splynx_customers') IS NOT NULL THEN
                UPDATE subscribers sub
                SET metadata = jsonb_strip_nulls(
                    (COALESCE(sub.metadata::jsonb, '{}'::jsonb)
                        || jsonb_build_object(
                            'splynx_date_add', to_jsonb(sc.date_add),
                            'splynx_last_update', to_jsonb(sc.last_update),
                            'splynx_conversion_date', to_jsonb(sc.conversion_date)
                        )
                    )
                )::json
                FROM public.splynx_customers sc
                WHERE sub.splynx_customer_id = sc.id
                  AND (
                      sc.date_add IS NOT NULL
                      OR sc.last_update IS NOT NULL
                      OR sc.conversion_date IS NOT NULL
                  );
            ELSIF to_regclass('splynx_staging.splynx_customers') IS NOT NULL THEN
                UPDATE subscribers sub
                SET metadata = jsonb_strip_nulls(
                    (COALESCE(sub.metadata::jsonb, '{}'::jsonb)
                        || jsonb_build_object(
                            'splynx_date_add', to_jsonb(sc.date_add),
                            'splynx_last_update', to_jsonb(sc.last_update),
                            'splynx_conversion_date', to_jsonb(sc.conversion_date)
                        )
                    )
                )::json
                FROM splynx_staging.splynx_customers sc
                WHERE sub.splynx_customer_id = sc.id
                  AND (
                      sc.date_add IS NOT NULL
                      OR sc.last_update IS NOT NULL
                      OR sc.conversion_date IS NOT NULL
                  );
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE subscribers
        SET metadata = (
            metadata::jsonb
            - 'splynx_date_add'
            - 'splynx_last_update'
            - 'splynx_conversion_date'
        )::json
        WHERE splynx_customer_id IS NOT NULL;
        """
    )
