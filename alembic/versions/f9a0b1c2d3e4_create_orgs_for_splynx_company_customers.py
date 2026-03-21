"""Create Organizations for Splynx company customers.

Splynx customers with category='company' were imported as Subscribers
but no Organization was created. This migration:
1. Creates an Organization for each company subscriber
2. Links the subscriber via organization_id
3. Sets primary_login_subscriber_id on the org

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-03-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f9a0b1c2d3e4"
down_revision: str = "e8f9a0b1c2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # Find all Splynx company customers that don't already have an organization
    company_subs = conn.execute(sa.text("""
        SELECT id, display_name, address_line1, city, postal_code, country_code,
               metadata->>'splynx_deleted' as splynx_deleted
        FROM subscribers
        WHERE splynx_customer_id IS NOT NULL
          AND metadata->>'splynx_category' = 'company'
          AND organization_id IS NULL
        ORDER BY splynx_customer_id
    """)).fetchall()

    if not company_subs:
        return

    created = 0
    for sub in company_subs:
        is_deleted = sub.splynx_deleted == "true"

        # Create Organization
        result = conn.execute(sa.text("""
            INSERT INTO organizations (id, name, address_line1, city, postal_code,
                                       country_code, is_active, primary_login_subscriber_id,
                                       created_at, updated_at)
            VALUES (gen_random_uuid(), :name, :addr, :city, :postal, :country,
                    :is_active, :sub_id, NOW(), NOW())
            RETURNING id
        """), {
            "name": (sub.display_name or "Unknown Org")[:160],
            "addr": sub.address_line1,
            "city": sub.city,
            "postal": sub.postal_code,
            "country": sub.country_code,
            "is_active": not is_deleted,
            "sub_id": sub.id,
        })
        org_id = result.scalar_one()

        # Link subscriber to organization
        conn.execute(sa.text("""
            UPDATE subscribers SET organization_id = :org_id WHERE id = :sub_id
        """), {"org_id": org_id, "sub_id": sub.id})

        created += 1

    op.execute("SELECT 1")  # ensure transaction is active
    print(f"  Created {created} organizations for Splynx company customers")


def downgrade() -> None:
    conn = op.get_bind()

    # Remove organization links for Splynx company customers
    conn.execute(sa.text("""
        UPDATE subscribers
        SET organization_id = NULL
        WHERE splynx_customer_id IS NOT NULL
          AND metadata->>'splynx_category' = 'company'
          AND organization_id IS NOT NULL
    """))

    # Delete organizations that were created for Splynx company customers
    # (identified by having a primary_login_subscriber that is a Splynx company customer)
    conn.execute(sa.text("""
        DELETE FROM organizations
        WHERE primary_login_subscriber_id IN (
            SELECT id FROM subscribers
            WHERE splynx_customer_id IS NOT NULL
              AND metadata->>'splynx_category' = 'company'
        )
    """))
