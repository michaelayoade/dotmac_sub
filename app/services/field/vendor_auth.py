"""Vendor-token context for field vendor routes."""

from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.vendor_routes import Vendor
from app.services.auth_dependencies import require_user_auth
from app.services.common import coerce_uuid


def _system_user_id(auth: dict) -> str:
    if auth.get("principal_type") != "system_user":
        raise HTTPException(status_code=403, detail="Vendor access required")
    return str(auth.get("principal_id") or auth.get("person_id") or "")


def vendor_context(db: Session, auth: dict) -> dict:
    system_user_id = coerce_uuid(_system_user_id(auth))
    membership = (
        db.query(FieldVendorUser)
        .join(FieldVendor, FieldVendor.id == FieldVendorUser.vendor_id)
        .filter(FieldVendorUser.system_user_id == system_user_id)
        .filter(FieldVendorUser.is_active.is_(True))
        .filter(FieldVendor.is_active.is_(True))
        .order_by(FieldVendorUser.created_at.desc())
        .first()
    )
    if membership is None:
        raise HTTPException(status_code=403, detail="Vendor access required")
    native_vendor = None
    if membership.vendor.crm_vendor_id:
        try:
            native_vendor = db.get(Vendor, coerce_uuid(membership.vendor.crm_vendor_id))
        except (TypeError, ValueError):
            native_vendor = None
    return {
        **auth,
        "vendor_user_id": str(membership.id),
        "vendor_id": str(membership.vendor_id),
        "vendor_role": membership.role,
        "vendor_user": membership,
        "vendor": membership.vendor,
        "native_vendor_id": str(native_vendor.id) if native_vendor else None,
        "native_vendor": native_vendor,
    }


def require_field_vendor_token(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> dict:
    return vendor_context(db, auth)


def require_native_vendor_context(
    vendor: dict = Depends(require_field_vendor_token),
) -> dict:
    if vendor.get("native_vendor") is None:
        raise HTTPException(
            status_code=409,
            detail="Vendor account is not linked to the native vendor domain",
        )
    return vendor


def require_scoped_permission(
    vendor: dict = Depends(require_native_vendor_context),
) -> dict:
    """Authorize a caller against the vendor account linked to their user."""
    return vendor
