"""Service helpers for admin ONU type catalog web routes."""

from __future__ import annotations

import logging

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.network import (
    GponChannel,
    OntProvisioningProfile,
    OnuCapability,
    OnuType,
    PonType,
    VendorModelCapability,
)
from app.services.common import coerce_uuid
from app.services.network.onu_types import onu_types

logger = logging.getLogger(__name__)


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value.strip() if isinstance(value, str) else default


def _form_int(form: FormData, key: str, default: int = 0) -> int:
    raw = _form_str(form, key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _form_uuid_str(form: FormData, key: str) -> str | None:
    value = _form_str(form, key)
    return value or None


def list_context(
    request: Request,
    db: Session,
    search: str | None = None,
    pon_type: str | None = None,
) -> dict[str, object]:
    """Return context dict for the ONU type list page."""
    from app.web.admin import get_current_user, get_sidebar_stats

    items = onu_types.list(db, search=search, pon_type=pon_type, is_active=None)
    return {
        "request": request,
        "active_page": "onu-types",
        "active_menu": "network",
        "items": items,
        "pon_types": [e.value for e in PonType],
        "search": search or "",
        "pon_type_filter": pon_type or "",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def form_context(
    request: Request,
    db: Session,
    onu_type_id: str | None = None,
) -> dict[str, object]:
    """Return context dict for the ONU type create/edit form."""
    from app.web.admin import get_current_user, get_sidebar_stats

    item = onu_types.get(db, onu_type_id) if onu_type_id else None
    vendor_capabilities = db.scalars(
        select(VendorModelCapability)
        .where(VendorModelCapability.is_active.is_(True))
        .order_by(VendorModelCapability.vendor, VendorModelCapability.model)
    ).all()
    provisioning_bundles = db.scalars(
        select(OntProvisioningProfile)
        .where(OntProvisioningProfile.is_active.is_(True))
        .order_by(OntProvisioningProfile.name)
    ).all()
    return {
        "request": request,
        "active_page": "onu-types",
        "active_menu": "network",
        "item": item,
        "pon_types": list(PonType),
        "gpon_channels": list(GponChannel),
        "capabilities": list(OnuCapability),
        "vendor_capabilities": vendor_capabilities,
        "provisioning_bundles": provisioning_bundles,
        "action_url": (
            f"/admin/network/onu-types/{onu_type_id}/edit"
            if onu_type_id
            else "/admin/network/onu-types/create"
        ),
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def parse_form_values(form: FormData) -> dict[str, object]:
    """Parse ONU type form fields into normalized values."""
    return {
        "name": _form_str(form, "name"),
        "pon_type": _form_str(form, "pon_type"),
        "gpon_channel": _form_str(form, "gpon_channel"),
        "ethernet_ports": _form_int(form, "ethernet_ports"),
        "wifi_ports": _form_int(form, "wifi_ports"),
        "voip_ports": _form_int(form, "voip_ports"),
        "catv_ports": _form_int(form, "catv_ports"),
        "allow_custom_profiles": _form_str(form, "allow_custom_profiles") == "true",
        "capability": _form_str(form, "capability"),
        "vendor_model_capability_id": _form_uuid_str(
            form, "vendor_model_capability_id"
        ),
        "default_bundle_id": _form_uuid_str(form, "default_bundle_id"),
        "supports_bundle_overrides": (
            _form_str(form, "supports_bundle_overrides") == "true"
        ),
        "notes": _form_str(form, "notes") or None,
    }


def validate_form(values: dict[str, object], db: Session | None = None) -> str | None:
    """Validate ONU type form values. Returns error message or None."""
    name = values.get("name")
    if not name or not str(name).strip():
        return "ONU type name is required."
    pon_type_val = values.get("pon_type")
    if not pon_type_val:
        return "PON type is required."
    try:
        PonType(str(pon_type_val))
    except ValueError:
        return f"Invalid PON type: {pon_type_val}"
    gpon_channel_val = values.get("gpon_channel")
    if not gpon_channel_val:
        return "GPON channel is required."
    try:
        GponChannel(str(gpon_channel_val))
    except ValueError:
        return f"Invalid GPON channel: {gpon_channel_val}"
    capability_val = values.get("capability")
    if not capability_val:
        return "Capability is required."
    try:
        OnuCapability(str(capability_val))
    except ValueError:
        return f"Invalid capability: {capability_val}"
    vendor_model_capability_id = values.get("vendor_model_capability_id")
    if vendor_model_capability_id:
        try:
            capability_id = coerce_uuid(str(vendor_model_capability_id))
        except ValueError:
            return "Invalid vendor capability map."
        if db and not db.get(VendorModelCapability, capability_id):
            return "Selected vendor capability map was not found."
    default_bundle_id = values.get("default_bundle_id")
    if default_bundle_id:
        try:
            bundle_id = coerce_uuid(str(default_bundle_id))
        except ValueError:
            return "Invalid default configuration bundle."
        if db and not db.get(OntProvisioningProfile, bundle_id):
            return "Selected default configuration bundle was not found."
    return None


def handle_create(db: Session, form_data: dict[str, object]) -> OnuType:
    """Create a new ONU type from validated form values."""
    return onu_types.create(
        db,
        name=str(form_data["name"]),
        pon_type=PonType(str(form_data["pon_type"])),
        gpon_channel=GponChannel(str(form_data["gpon_channel"])),
        ethernet_ports=int(form_data.get("ethernet_ports") or 0),
        wifi_ports=int(form_data.get("wifi_ports") or 0),
        voip_ports=int(form_data.get("voip_ports") or 0),
        catv_ports=int(form_data.get("catv_ports") or 0),
        allow_custom_profiles=bool(form_data.get("allow_custom_profiles", True)),
        capability=OnuCapability(str(form_data["capability"])),
        vendor_model_capability_id=(
            coerce_uuid(str(form_data["vendor_model_capability_id"]))
            if form_data.get("vendor_model_capability_id")
            else None
        ),
        default_bundle_id=(
            coerce_uuid(str(form_data["default_bundle_id"]))
            if form_data.get("default_bundle_id")
            else None
        ),
        supports_bundle_overrides=bool(form_data.get("supports_bundle_overrides")),
        notes=str(form_data["notes"]) if form_data.get("notes") else None,
    )


def handle_update(
    db: Session, onu_type_id: str, form_data: dict[str, object]
) -> OnuType:
    """Update an ONU type from validated form values."""
    return onu_types.update(
        db,
        onu_type_id,
        name=str(form_data["name"]),
        pon_type=PonType(str(form_data["pon_type"])),
        gpon_channel=GponChannel(str(form_data["gpon_channel"])),
        ethernet_ports=int(form_data.get("ethernet_ports") or 0),
        wifi_ports=int(form_data.get("wifi_ports") or 0),
        voip_ports=int(form_data.get("voip_ports") or 0),
        catv_ports=int(form_data.get("catv_ports") or 0),
        allow_custom_profiles=bool(form_data.get("allow_custom_profiles", True)),
        capability=OnuCapability(str(form_data["capability"])),
        vendor_model_capability_id=(
            coerce_uuid(str(form_data["vendor_model_capability_id"]))
            if form_data.get("vendor_model_capability_id")
            else None
        ),
        default_bundle_id=(
            coerce_uuid(str(form_data["default_bundle_id"]))
            if form_data.get("default_bundle_id")
            else None
        ),
        supports_bundle_overrides=bool(form_data.get("supports_bundle_overrides")),
        notes=str(form_data["notes"]) if form_data.get("notes") else None,
    )


def handle_delete(db: Session, onu_type_id: str) -> None:
    """Soft-delete an ONU type."""
    onu_types.delete(db, onu_type_id)
