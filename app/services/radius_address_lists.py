"""Canonical RADIUS/MikroTik address-list names."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec

DEFAULT_SUSPENDED_ADDRESS_LIST = "suspended"


def suspended_address_list(db: Session | None = None) -> str:
    if db is None:
        return DEFAULT_SUSPENDED_ADDRESS_LIST
    value = settings_spec.resolve_value(
        db, SettingDomain.radius, "suspended_address_list"
    )
    name = str(value or "").strip()
    return name or DEFAULT_SUSPENDED_ADDRESS_LIST
