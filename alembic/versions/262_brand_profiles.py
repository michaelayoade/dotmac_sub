"""Add canonical scoped brand profiles and backfill the platform brand.

Revision ID: 262_brand_profiles
Revises: 261_system_user_role_source
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "262_brand_profiles"
down_revision = "261_system_user_role_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brand_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("scope_type", sa.String(24), nullable=False),
        sa.Column("scope_id", sa.UUID()),
        sa.Column("brand_name", sa.String(120)),
        sa.Column("product_name", sa.String(160)),
        sa.Column("legal_name", sa.String(200)),
        sa.Column("tagline", sa.String(255)),
        sa.Column("primary_color", sa.String(7)),
        sa.Column("secondary_color", sa.String(7)),
        sa.Column("logo_url", sa.Text()),
        sa.Column("dark_logo_url", sa.Text()),
        sa.Column("favicon_url", sa.Text()),
        sa.Column("support_email", sa.String(255)),
        sa.Column("support_phone", sa.String(40)),
        sa.Column("from_email", sa.String(255)),
        sa.Column("from_name", sa.String(160)),
        sa.Column("app_url", sa.String(512)),
        sa.Column("portal_domain", sa.String(255)),
        sa.Column("legal_address", sa.JSON()),
        sa.Column("metadata", sa.JSON()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "(scope_type = 'platform' AND scope_id IS NULL) OR "
            "(scope_type IN ('reseller', 'organization') AND scope_id IS NOT NULL)",
            name="ck_brand_profiles_scope",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_brand_profiles_platform",
        "brand_profiles",
        ["scope_type"],
        unique=True,
        postgresql_where=sa.text("scope_id IS NULL"),
    )
    op.create_index(
        "uq_brand_profiles_scoped",
        "brand_profiles",
        ["scope_type", "scope_id"],
        unique=True,
        postgresql_where=sa.text("scope_id IS NOT NULL"),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO brand_profiles (
                id, scope_type, brand_name, product_name, legal_name, tagline,
                primary_color, secondary_color, logo_url, dark_logo_url,
                favicon_url, support_email, support_phone, from_email,
                from_name, app_url, legal_address, is_active,
                created_at, updated_at
            )
            SELECT
                gen_random_uuid(),
                'platform',
                NULL,
                NULLIF((SELECT value_text FROM domain_settings
                    WHERE domain = 'billing' AND key = 'company_name'
                      AND is_active = true LIMIT 1), ''),
                NULLIF((SELECT value_text FROM domain_settings
                    WHERE domain = 'billing' AND key = 'company_name'
                      AND is_active = true LIMIT 1), ''),
                NULL,
                NULLIF((SELECT value_text FROM domain_settings
                    WHERE domain = 'comms' AND key = 'brand_primary_color'
                      AND is_active = true LIMIT 1), ''),
                NULLIF((SELECT value_text FROM domain_settings
                    WHERE domain = 'comms' AND key = 'brand_secondary_color'
                      AND is_active = true LIMIT 1), ''),
                COALESCE((SELECT value_text FROM domain_settings
                    WHERE domain = 'comms' AND key = 'sidebar_logo_url'
                      AND is_active = true LIMIT 1), ''),
                COALESCE((SELECT value_text FROM domain_settings
                    WHERE domain = 'comms' AND key = 'sidebar_logo_dark_url'
                      AND is_active = true LIMIT 1), ''),
                COALESCE((SELECT value_text FROM domain_settings
                    WHERE domain = 'comms' AND key = 'favicon_url'
                      AND is_active = true LIMIT 1), ''),
                NULLIF((SELECT value_text FROM domain_settings
                    WHERE domain = 'billing' AND key = 'company_email'
                      AND is_active = true LIMIT 1), ''),
                COALESCE((SELECT value_text FROM domain_settings
                    WHERE domain = 'billing' AND key = 'company_phone'
                      AND is_active = true LIMIT 1), ''),
                NULL, NULL, NULL,
                json_build_object(
                    'street1', COALESCE((SELECT value_text FROM domain_settings
                        WHERE domain = 'billing' AND key = 'company_address_street1'
                          AND is_active = true LIMIT 1), ''),
                    'street2', COALESCE((SELECT value_text FROM domain_settings
                        WHERE domain = 'billing' AND key = 'company_address_street2'
                          AND is_active = true LIMIT 1), ''),
                    'city', COALESCE((SELECT value_text FROM domain_settings
                        WHERE domain = 'billing' AND key = 'company_address_city'
                          AND is_active = true LIMIT 1), ''),
                    'postal_code', COALESCE((SELECT value_text FROM domain_settings
                        WHERE domain = 'billing' AND key = 'company_address_zip'
                          AND is_active = true LIMIT 1), ''),
                    'country', COALESCE((SELECT value_text FROM domain_settings
                        WHERE domain = 'billing' AND key = 'company_address_country'
                          AND is_active = true LIMIT 1), '')
                ),
                true, now(), now()
            WHERE NOT EXISTS (
                SELECT 1 FROM brand_profiles WHERE scope_type = 'platform'
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_index("uq_brand_profiles_scoped", table_name="brand_profiles")
    op.drop_index("uq_brand_profiles_platform", table_name="brand_profiles")
    op.drop_table("brand_profiles")
