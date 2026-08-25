"""Relocate platform legal-identity settings to their owning domain.

The company-info admin page wrote ~15 unregistered keys into the ``billing``
domain. Eight of them — legal name, support email/phone and the five postal
address parts — are the settings-shaped inputs behind ``BrandProfile``, owned by
``customer.branding`` (``app.services.brand_profiles``). They are now registered
``SettingSpec`` entries in the ``comms`` domain, beside the sibling legacy
branding convergence inputs (``sidebar_logo_url``, ``favicon_url``,
``brand_primary_color``). This moves the stored rows to match.

Cutover, not expand: the read path (``settings_spec.resolve_value`` on ``comms``)
and the write path move together in the same change, so leaving a copy in
``billing`` would recreate the parallel source this slice removes. The move is
idempotent and skips any key that already has a ``comms`` row, so a
partially-migrated database converges rather than colliding with the
``uq_domain_settings_domain_key`` unique constraint.

Rows for the six retired keys (``company_registration_id``,
``company_bank_name``, ``company_bank_account``, ``company_bank_branch``,
``billing_url``, ``partner_commission_pct``) are deliberately left untouched in
``billing`` as migration evidence; only their write path was deleted.

Lock budget: trivial single-row UPDATEs on ``domain_settings``.
Downgrade moves the same eight keys back to ``billing``.

Revision ID: 462_company_identity_settings_comms_owner
Revises: 461_outage_incident_ticket_links
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "462_company_identity_settings_comms_owner"
down_revision = "461_outage_incident_ticket_links"
branch_labels = None
depends_on = None

BRAND_IDENTITY_KEYS = (
    "company_name",
    "company_address_street1",
    "company_address_street2",
    "company_address_city",
    "company_address_zip",
    "company_address_country",
    "company_email",
    "company_phone",
)

_MOVE = sa.text(
    """
    UPDATE domain_settings AS source
       SET domain = CAST(:target_domain AS settingdomain)
     WHERE source.domain = CAST(:source_domain AS settingdomain)
       AND source.key = :key
       AND NOT EXISTS (
           SELECT 1
             FROM domain_settings AS existing
            WHERE existing.domain = CAST(:target_domain AS settingdomain)
              AND existing.key = :key
       )
    """
)


def _move(source_domain: str, target_domain: str) -> None:
    bind = op.get_bind()
    for key in BRAND_IDENTITY_KEYS:
        bind.execute(
            _MOVE,
            {
                "source_domain": source_domain,
                "target_domain": target_domain,
                "key": key,
            },
        )


def upgrade() -> None:
    _move("billing", "comms")


def downgrade() -> None:
    _move("comms", "billing")
