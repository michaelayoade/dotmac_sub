"""Service helpers for company information settings page."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services.domain_settings import billing_settings
from app.services.settings_spec import get_spec, read_stored_value, resolve_value

logger = logging.getLogger(__name__)

COMPANY_KEYS = [
    "company_name",
    "company_address_street1",
    "company_address_street2",
    "company_address_city",
    "company_address_zip",
    "company_address_country",
    "company_email",
    "company_phone",
    "company_vat_number",
    "company_registration_id",
    "company_bank_name",
    "company_bank_account",
    "company_bank_branch",
    "billing_url",
    "partner_commission_pct",
]


def get_company_info(db: Session) -> dict[str, str]:
    """Read all company-related settings from the billing domain."""
    result: dict[str, str] = {}
    for key in COMPANY_KEYS:
        value = (
            resolve_value(db, SettingDomain.billing, key)
            if get_spec(SettingDomain.billing, key)
            else read_stored_value(db, SettingDomain.billing, key)
        )
        result[key] = str(value or "")
    return result


def save_company_info(db: Session, data: Mapping[str, Any]) -> None:
    """Upsert company information settings."""
    billing_settings.upsert_many_by_key(
        db,
        {
            key: DomainSettingUpdate(
                value_text=str(data.get(key) or "").strip(),
                value_type=SettingValueType.string,
                is_active=True,
            )
            for key in COMPANY_KEYS
        },
    )
