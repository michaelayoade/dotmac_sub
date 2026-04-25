"""Service helpers for admin authorization preset web routes."""

from __future__ import annotations

import logging
import re

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.network import (
    AuthorizationPreset,
    OLTDevice,
    Vlan,
)
from app.services.network.authorization_presets import authorization_presets

logger = logging.getLogger(__name__)


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value.strip() if isinstance(value, str) else default


def _form_int(form: FormData, key: str, default: int | None = None) -> int | None:
    raw = _form_str(form, key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _form_bool(form: FormData, key: str) -> bool:
    return _form_str(form, key) == "true"


def _active_olts(db: Session) -> list[OLTDevice]:
    stmt = (
        select(OLTDevice).where(OLTDevice.is_active.is_(True)).order_by(OLTDevice.name)
    )
    return list(db.scalars(stmt).all())


def _active_vlans(db: Session) -> list[Vlan]:
    stmt = (
        select(Vlan)
        .where(Vlan.is_active.is_(True))
        .order_by(Vlan.olt_device_id.nulls_first(), Vlan.tag)
    )
    return list(db.scalars(stmt).all())


def list_context(
    request: Request,
    db: Session,
    search: str | None = None,
    olt_device_id: str | None = None,
    is_active: str | None = None,
) -> dict[str, object]:
    """Return context dict for the authorization preset list page."""
    from app.web.admin import get_current_user, get_sidebar_stats

    # Parse is_active filter
    active_filter: bool | None = None
    if is_active == "true":
        active_filter = True
    elif is_active == "false":
        active_filter = False

    items = authorization_presets.list(
        db,
        is_active=active_filter,
        olt_device_id=olt_device_id,
        include_global=True,
    )

    # Filter by search if provided
    if search:
        search_lower = search.lower()
        items = [
            item
            for item in items
            if search_lower in (item.name or "").lower()
            or search_lower in (item.description or "").lower()
            or search_lower in (item.serial_pattern or "").lower()
        ]

    return {
        "request": request,
        "active_page": "authorization-presets",
        "active_menu": "network",
        "items": items,
        "olt_devices": _active_olts(db),
        "search": search or "",
        "olt_device_id_filter": olt_device_id or "",
        "is_active_filter": is_active or "",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def form_context(
    request: Request,
    db: Session,
    preset_id: str | None = None,
) -> dict[str, object]:
    """Return context dict for the create/edit form."""
    from app.web.admin import get_current_user, get_sidebar_stats

    item = authorization_presets.get(db, preset_id) if preset_id else None

    return {
        "request": request,
        "active_page": "authorization-presets",
        "active_menu": "network",
        "item": item,
        "olt_devices": _active_olts(db),
        "vlans": _active_vlans(db),
        "action_url": (
            f"/admin/network/authorization-presets/{preset_id}/edit"
            if preset_id
            else "/admin/network/authorization-presets/create"
        ),
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def parse_preset_form(form: FormData) -> dict[str, object]:
    """Parse preset form fields into normalized values."""
    return {
        "name": _form_str(form, "name"),
        "description": _form_str(form, "description") or None,
        "line_profile_id": _form_int(form, "line_profile_id"),
        "service_profile_id": _form_int(form, "service_profile_id"),
        "default_vlan_id": _form_str(form, "default_vlan_id") or None,
        "auto_authorize": _form_bool(form, "auto_authorize"),
        "serial_pattern": _form_str(form, "serial_pattern") or None,
        "olt_device_id": _form_str(form, "olt_device_id") or None,
        "priority": _form_int(form, "priority", 0),
        "is_active": _form_bool(form, "is_active"),
        "is_default": _form_bool(form, "is_default"),
    }


def validate_preset_form(values: dict[str, object]) -> str | None:
    """Validate preset form values. Returns error message or None."""
    name = values.get("name")
    if not name or not str(name).strip():
        return "Preset name is required."

    # Validate serial pattern regex if provided
    serial_pattern = values.get("serial_pattern")
    if serial_pattern:
        try:
            re.compile(str(serial_pattern))
        except re.error as e:
            return f"Invalid regex pattern: {e}"

    # Validate line/service profile IDs are both set or both unset
    line_profile_id = values.get("line_profile_id")
    service_profile_id = values.get("service_profile_id")
    if (line_profile_id is not None) != (service_profile_id is not None):
        return "Both line profile ID and service profile ID must be provided together."

    return None


def handle_create(
    request: Request, db: Session, form_data: dict[str, object]
) -> AuthorizationPreset:
    """Create a new authorization preset from validated form values."""
    return authorization_presets.create(
        db,
        name=str(form_data["name"]),
        description=str(form_data["description"]) if form_data.get("description") else None,
        line_profile_id=int(str(form_data["line_profile_id"]))
        if form_data.get("line_profile_id") is not None
        else None,
        service_profile_id=int(str(form_data["service_profile_id"]))
        if form_data.get("service_profile_id") is not None
        else None,
        default_vlan_id=str(form_data["default_vlan_id"])
        if form_data.get("default_vlan_id")
        else None,
        auto_authorize=bool(form_data.get("auto_authorize")),
        serial_pattern=str(form_data["serial_pattern"])
        if form_data.get("serial_pattern")
        else None,
        olt_device_id=str(form_data["olt_device_id"])
        if form_data.get("olt_device_id")
        else None,
        priority=int(str(form_data.get("priority") or 0)),
        is_active=bool(form_data.get("is_active")),
        is_default=bool(form_data.get("is_default")),
    )


def handle_update(
    request: Request,
    db: Session,
    preset_id: str,
    form_data: dict[str, object],
) -> AuthorizationPreset | None:
    """Update an authorization preset from validated form values."""
    return authorization_presets.update(
        db,
        preset_id,
        name=str(form_data["name"]),
        description=str(form_data["description"]) if form_data.get("description") else None,
        line_profile_id=int(str(form_data["line_profile_id"]))
        if form_data.get("line_profile_id") is not None
        else None,
        service_profile_id=int(str(form_data["service_profile_id"]))
        if form_data.get("service_profile_id") is not None
        else None,
        default_vlan_id=str(form_data["default_vlan_id"])
        if form_data.get("default_vlan_id")
        else None,
        auto_authorize=bool(form_data.get("auto_authorize")),
        serial_pattern=str(form_data["serial_pattern"])
        if form_data.get("serial_pattern")
        else None,
        olt_device_id=str(form_data["olt_device_id"])
        if form_data.get("olt_device_id")
        else None,
        priority=int(str(form_data.get("priority") or 0)),
        is_active=bool(form_data.get("is_active")),
        is_default=bool(form_data.get("is_default")),
        clear_default_vlan=not form_data.get("default_vlan_id"),
        clear_olt_device=not form_data.get("olt_device_id"),
    )


def handle_delete(db: Session, preset_id: str) -> None:
    """Delete an authorization preset."""
    authorization_presets.delete(db, preset_id)
