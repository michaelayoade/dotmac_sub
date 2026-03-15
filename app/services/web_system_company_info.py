"""Service helpers for company information settings page."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType

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
    stmt = (
        select(DomainSetting)
        .where(DomainSetting.domain == SettingDomain.billing)
        .where(DomainSetting.key.in_(COMPANY_KEYS))
    )
    rows = db.scalars(stmt).all()
    result: dict[str, str] = dict.fromkeys(COMPANY_KEYS, "")
    for row in rows:
        result[row.key] = row.value_text or ""
    return result


def save_company_info(db: Session, data: Mapping[str, Any]) -> None:
    """Upsert company information settings."""
    for key in COMPANY_KEYS:
        value = (data.get(key) or "").strip()
        stmt = select(DomainSetting).where(
            DomainSetting.domain == SettingDomain.billing,
            DomainSetting.key == key,
        )
        setting = db.scalars(stmt).first()
        if setting:
            setting.value_text = value
        else:
            setting = DomainSetting(
                domain=SettingDomain.billing,
                key=key,
                value_text=value,
                value_type=SettingValueType.string,
            )
            db.add(setting)
    db.flush()
    db.commit()
