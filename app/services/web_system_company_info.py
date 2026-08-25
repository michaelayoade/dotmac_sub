"""Company information admin surface, resolved through the settings registry.

This module used to write ~15 unregistered ``company_*``/``billing_url``/
``partner_commission_pct`` rows into the ``billing`` domain with raw ORM
statements: a second settings surface with no ``SettingSpec``, no validation
and no resolver. The keys were never one concern — they mixed legal identity,
tax registration, banking, a billing URL and commission policy.

What each group is now:

``BRAND_IDENTITY_KEYS``
    Legal identity, contact and postal address of the operating entity. Owner:
    ``customer.branding`` (``app.services.brand_profiles``), which already owns
    "legacy branding convergence" and projects these values onto
    ``BrandProfile.legal_name`` / ``.support_email`` / ``.support_phone`` /
    ``.legal_address``. Registered as ``comms``-domain ``SettingSpec`` entries
    beside the sibling convergence inputs (logo, favicon, brand colours) and
    resolved through ``settings_spec.resolve_value``.

``UNOWNED_TAX_IDENTITY_KEYS``
    ``company_vat_number`` is the operating entity's own tax-registration
    identity. Its single consumer is the invoice tax label
    (``app.services.billing_invoice_pdf``), but no registered owner covers it:
    ``customer.branding`` owns branding (``BrandProfile`` has no tax field) and
    ``financial.tax_configuration`` owns tax-*rate* records, not the operator's
    registration number. It is deliberately left unregistered and in the
    ``billing`` domain pending an explicit ownership decision, rather than
    assigned to a domain by guess. It is written through the domain-settings
    service like every other key here, so no raw-ORM settings path remains.

Deleted (zero consumers anywhere in ``app/``, ``templates/`` or ``scripts/``):
``company_registration_id``, ``company_bank_name``, ``company_bank_account``,
``company_bank_branch``, ``billing_url``, ``partner_commission_pct``. The bank
fields were already retired as an invoice fallback by
``tests/architecture/test_collection_account_ownership.py`` — the owner of a
receiving account is ``financial.collection_accounts``. Per-partner commission
is carried by ``Organization.commission_rate``, not a global setting. Existing
rows are left in place as migration evidence; only the write path is removed,
and ``tests/architecture/test_company_identity_settings_ownership.py`` keeps
them from coming back.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import settings_spec
from app.services.domain_settings import billing_settings, comms_settings
from app.services.settings_cache import SettingsCache

logger = logging.getLogger(__name__)

# Owner: customer.branding (app.services.brand_profiles). Registered in the
# comms domain — see app/services/settings_spec.py.
BRAND_IDENTITY_KEYS: tuple[str, ...] = (
    "company_name",
    "company_address_street1",
    "company_address_street2",
    "company_address_city",
    "company_address_zip",
    "company_address_country",
    "company_email",
    "company_phone",
)

# Owner undecided; see the module docstring. Still stored in the billing domain
# and still unregistered, so it must be read from the row rather than resolved.
UNOWNED_TAX_IDENTITY_KEYS: tuple[str, ...] = ("company_vat_number",)

COMPANY_KEYS: tuple[str, ...] = BRAND_IDENTITY_KEYS + UNOWNED_TAX_IDENTITY_KEYS


def get_company_info(db: Session) -> dict[str, str]:
    """Read company information from its owning settings surfaces."""
    result: dict[str, str] = {}
    for key in BRAND_IDENTITY_KEYS:
        value = settings_spec.resolve_value(db, SettingDomain.comms, key)
        result[key] = str(value) if value is not None else ""
    for key in UNOWNED_TAX_IDENTITY_KEYS:
        setting = billing_settings.get_optional_by_key(db, key)
        result[key] = (setting.value_text or "") if setting else ""
    return result


def save_company_info(db: Session, data: Mapping[str, Any]) -> None:
    """Upsert company information through the domain-settings service."""
    for domain_settings_service, keys in (
        (comms_settings, BRAND_IDENTITY_KEYS),
        (billing_settings, UNOWNED_TAX_IDENTITY_KEYS),
    ):
        for key in keys:
            value = (data.get(key) or "").strip()
            domain_settings_service.stage_upsert_by_key(
                db,
                key,
                DomainSettingUpdate(
                    value_type=SettingValueType.string,
                    value_text=value,
                    is_active=True,
                ),
            )
    from app.services.brand_profiles import sync_platform_brand_from_legacy_settings

    sync_platform_brand_from_legacy_settings(
        db,
        overwrite_fields={
            "product_name",
            "legal_name",
            "support_email",
            "support_phone",
            "legal_address",
        },
    )
    db.commit()
    # The brand sync resolves the staged values before the commit lands, which
    # can populate the cache from an uncommitted read. Drop those entries so the
    # next resolve reads the committed row.
    for key in BRAND_IDENTITY_KEYS:
        SettingsCache.invalidate(SettingDomain.comms.value, key)
