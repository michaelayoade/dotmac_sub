"""Public branding asset helpers."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.branding import BrandProfile
from app.models.domain_settings import DomainSetting, SettingDomain


def is_configured_favicon_url(db: Session, file_id: uuid.UUID) -> bool:
    legacy_match = (
        db.query(DomainSetting.id)
        .filter(DomainSetting.domain == SettingDomain.comms)
        .filter(DomainSetting.key == "favicon_url")
        .filter(DomainSetting.value_text == f"/branding/assets/{file_id}")
        .filter(DomainSetting.is_active.is_(True))
        .first()
        is not None
    )
    if legacy_match:
        return True
    return (
        db.query(BrandProfile.id)
        .filter(BrandProfile.favicon_url == f"/branding/assets/{file_id}")
        .filter(BrandProfile.is_active.is_(True))
        .first()
        is not None
    )
