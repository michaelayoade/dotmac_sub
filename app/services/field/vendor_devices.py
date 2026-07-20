"""Vendor field push-device registry."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.field_vendor import FieldVendorDeviceToken
from app.services.common import coerce_uuid


def register_vendor_device(
    db: Session,
    *,
    vendor_user_id: str,
    token: str,
    platform: str | None = None,
    app_version: str | None = None,
) -> FieldVendorDeviceToken:
    vendor_user_uuid = coerce_uuid(vendor_user_id)
    existing = (
        db.query(FieldVendorDeviceToken)
        .filter(FieldVendorDeviceToken.token == token)
        .first()
    )
    if existing is not None:
        existing.vendor_user_id = vendor_user_uuid
        existing.platform = platform or existing.platform
        existing.app_version = app_version or existing.app_version
        existing.is_active = True
        existing.last_seen_at = datetime.now(UTC)
        db.commit()
        db.refresh(existing)
        return existing
    row = FieldVendorDeviceToken(
        vendor_user_id=vendor_user_uuid,
        token=token,
        platform=platform,
        app_version=app_version,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
