"""Map confirmed Karu customers to Karu BTS.

Revision ID: 208_map_karu_customers_to_bts
Revises: 207_add_cutover_balance_variances
Create Date: 2026-07-04
"""

from __future__ import annotations

from alembic import op

revision = "208_map_karu_customers_to_bts"
down_revision = "207_add_cutover_balance_variances"
branch_labels = None
depends_on = None


_KARU_MATCH_SQL = """
    s.crm_subscriber_id IS NOT NULL
    AND s.user_type = 'customer'
    AND s.status IN ('active', 'blocked', 'suspended')
    AND (
        concat_ws(
            ' ',
            s.display_name,
            s.first_name,
            s.last_name,
            s.company_name,
            s.email,
            s.phone,
            s.address_line1,
            s.address_line2,
            s.city,
            s.region,
            s.notes
        ) ILIKE '%karu%'
        OR EXISTS (
            SELECT 1
            FROM addresses a
            WHERE a.subscriber_id = s.id
              AND concat_ws(
                    ' ',
                    a.address_line1,
                    a.address_line2,
                    a.city,
                    a.region
                  ) ILIKE '%karu%'
        )
    )
"""


def upgrade() -> None:
    # Karu customers are served by AFR Access, but must be targetable by the
    # Karu BTS/location filter for customer communications.
    op.execute(
        f"""
        WITH karu_bts AS (
            SELECT id
            FROM pop_sites
            WHERE lower(name) = 'karu bts'
              AND is_active IS TRUE
            LIMIT 1
        ),
        karu_customers AS (
            SELECT s.id
            FROM subscribers s
            WHERE {_KARU_MATCH_SQL}
        )
        UPDATE subscribers s
        SET pop_site_id = (SELECT id FROM karu_bts),
            updated_at = now()
        WHERE s.id IN (SELECT id FROM karu_customers)
          AND EXISTS (SELECT 1 FROM karu_bts)
          AND s.pop_site_id IS DISTINCT FROM (SELECT id FROM karu_bts);
        """
    )

    op.execute(
        f"""
        WITH afr_access AS (
            SELECT id
            FROM nas_devices
            WHERE lower(name) = 'afr access'
              AND is_active IS TRUE
              AND status = 'active'
            LIMIT 1
        ),
        karu_customers AS (
            SELECT s.id
            FROM subscribers s
            WHERE {_KARU_MATCH_SQL}
        )
        UPDATE subscriptions sub
        SET provisioning_nas_device_id = (SELECT id FROM afr_access),
            updated_at = now()
        WHERE sub.subscriber_id IN (SELECT id FROM karu_customers)
          AND sub.status IN ('pending', 'active', 'blocked', 'suspended', 'stopped')
          AND EXISTS (SELECT 1 FROM afr_access)
          AND sub.provisioning_nas_device_id IS DISTINCT FROM (SELECT id FROM afr_access);
        """
    )


def downgrade() -> None:
    # This is an operational data correction. Reversing it would require the
    # previous customer/NAS mappings, which are intentionally not guessed here.
    pass
