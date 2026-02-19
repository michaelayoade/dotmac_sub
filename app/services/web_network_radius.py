"""Service helpers for admin RADIUS server/client web routes."""

from __future__ import annotations

import hashlib
from typing import cast

from sqlalchemy.orm import Session

from app.models.catalog import NasVendor
from app.models.radius import RadiusServer
from app.schemas.catalog import (
    RadiusAttributeCreate,
    RadiusProfileCreate,
    RadiusProfileUpdate,
)
from app.schemas.radius import (
    RadiusClientCreate,
    RadiusClientUpdate,
    RadiusServerCreate,
    RadiusServerUpdate,
)
from app.services import catalog as catalog_service
from app.services import radius as radius_service
from app.services.audit_helpers import diff_dicts, model_to_dict
from app.services.common import coerce_uuid


def active_servers(db: Session) -> list[RadiusServer]:
    """Return active RADIUS servers for client form select options."""
    return cast(
        list[RadiusServer],
        db.query(RadiusServer)
        .filter(RadiusServer.is_active.is_(True))
        .order_by(RadiusServer.name)
        .all(),
    )


def parse_server_form(form) -> dict[str, object]:
    """Parse RADIUS server form values."""
    return {
        "name": form.get("name", "").strip(),
        "host": form.get("host", "").strip(),
        "auth_port_raw": form.get("auth_port", "").strip(),
        "acct_port_raw": form.get("acct_port", "").strip(),
        "description": form.get("description", "").strip(),
        "is_active": form.get("is_active") == "true",
    }


def validate_server_form(values: dict[str, object]) -> str | None:
    """Validate required RADIUS server fields."""
    if not values.get("name"):
        return "Server name is required."
    if not values.get("host"):
        return "Host is required."
    return None


def build_server_payload(values: dict[str, object], *, current_server) -> tuple[RadiusServerUpdate | None, str | None]:
    """Build server update payload and validate optional ports."""
    auth_port = current_server.auth_port
    acct_port = current_server.acct_port
    auth_port_raw = str(values.get("auth_port_raw") or "")
    acct_port_raw = str(values.get("acct_port_raw") or "")
    try:
        if auth_port_raw:
            auth_port = int(auth_port_raw)
        if acct_port_raw:
            acct_port = int(acct_port_raw)
    except ValueError:
        return None, "Auth and accounting ports must be valid integers."
    return (
        RadiusServerUpdate(
            name=str(values.get("name") or ""),
            host=str(values.get("host") or ""),
            auth_port=auth_port,
            acct_port=acct_port,
            description=(str(values.get("description") or "") or None),
            is_active=bool(values.get("is_active")),
        ),
        None,
    )


def server_form_data(values: dict[str, object], *, current_server) -> dict[str, object]:
    """Build template-friendly server data for re-renders."""
    auth_port_raw = str(values.get("auth_port_raw") or "")
    acct_port_raw = str(values.get("acct_port_raw") or "")
    return {
        "id": current_server.id,
        "name": values.get("name"),
        "host": values.get("host"),
        "auth_port": auth_port_raw or str(current_server.auth_port),
        "acct_port": acct_port_raw or str(current_server.acct_port),
        "description": (str(values.get("description") or "") or None),
        "is_active": bool(values.get("is_active")),
    }


def build_server_create_payload(values: dict[str, object]) -> tuple[RadiusServerCreate | None, str | None]:
    auth_port_raw = str(values.get("auth_port_raw") or "")
    acct_port_raw = str(values.get("acct_port_raw") or "")
    try:
        auth_port = int(auth_port_raw) if auth_port_raw else 1812
        acct_port = int(acct_port_raw) if acct_port_raw else 1813
    except ValueError:
        return None, "Auth and accounting ports must be valid integers."
    return (
        RadiusServerCreate(
            name=str(values.get("name") or ""),
            host=str(values.get("host") or ""),
            auth_port=auth_port,
            acct_port=acct_port,
            description=(str(values.get("description") or "") or None),
            is_active=bool(values.get("is_active")),
        ),
        None,
    )


def server_create_form_data(values: dict[str, object]) -> dict[str, object]:
    return {
        "name": values.get("name"),
        "host": values.get("host"),
        "auth_port": str(values.get("auth_port_raw") or "1812"),
        "acct_port": str(values.get("acct_port_raw") or "1813"),
        "description": (str(values.get("description") or "") or None),
        "is_active": bool(values.get("is_active")),
    }


def parse_client_form(form) -> dict[str, object]:
    """Parse RADIUS client form values."""
    return {
        "server_id": form.get("server_id", "").strip(),
        "client_ip": form.get("client_ip", "").strip(),
        "shared_secret": form.get("shared_secret", ""),
        "description": form.get("description", "").strip(),
        "is_active": form.get("is_active") == "true",
    }


def validate_client_form(values: dict[str, object], *, require_secret: bool) -> str | None:
    """Validate required client fields."""
    if not values.get("server_id"):
        return "RADIUS server is required."
    if not values.get("client_ip"):
        return "Client IP address is required."
    if require_secret and not values.get("shared_secret"):
        return "Shared secret is required."
    return None


def build_client_update_payload(values: dict[str, object]) -> RadiusClientUpdate:
    """Build client update payload."""
    server_id_raw = str(values.get("server_id") or "")
    payload_data = {
        "server_id": coerce_uuid(server_id_raw) if server_id_raw else None,
        "client_ip": str(values.get("client_ip") or "") or None,
        "description": (str(values.get("description") or "") or None),
        "is_active": bool(values.get("is_active")),
    }
    shared_secret = str(values.get("shared_secret") or "")
    if shared_secret:
        payload_data["shared_secret_hash"] = hashlib.sha256(shared_secret.encode("utf-8")).hexdigest()
    return RadiusClientUpdate.model_validate(payload_data)


def client_form_data(values: dict[str, object], *, client_id: str | None = None) -> dict[str, object]:
    """Build template-friendly client form data for re-renders."""
    data = {
        "server_id": values.get("server_id"),
        "client_ip": values.get("client_ip"),
        "description": (str(values.get("description") or "") or None),
        "is_active": bool(values.get("is_active")),
    }
    if client_id:
        data["id"] = client_id
    return data


def radius_page_data(db) -> dict[str, object]:
    servers = radius_service.radius_servers.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    clients = radius_service.radius_clients.list(
        db=db,
        server_id=None,
        is_active=None,
        order_by="client_ip",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    profiles = catalog_service.radius_profiles.list(
        db=db,
        vendor=None,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    return {
        "profiles": profiles,
        "servers": servers,
        "clients": clients,
    }


def profile_vendors() -> list[str]:
    return [item.value for item in NasVendor]


def profile_new_form_data() -> dict[str, object]:
    return {
        "profile": None,
        "attributes": [],
        "vendors": profile_vendors(),
        "action_url": "/admin/network/radius/profiles",
    }


def profile_edit_form_data(db, profile_id: str) -> dict[str, object] | None:
    try:
        profile = catalog_service.radius_profiles.get(db=db, profile_id=profile_id)
    except Exception:
        return None
    attributes = catalog_service.radius_attributes.list(
        db=db,
        profile_id=profile_id,
        order_by="attribute",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    return {
        "profile": profile,
        "attributes": attributes,
        "vendors": profile_vendors(),
        "action_url": f"/admin/network/radius/profiles/{profile_id}",
    }


def parse_profile_attributes(form) -> tuple[list[dict], str | None]:
    names = form.getlist("attribute_name")
    operators = form.getlist("attribute_operator")
    values = form.getlist("attribute_value")
    max_len = max(len(names), len(operators), len(values))
    attrs: list[dict] = []
    for idx in range(max_len):
        name = (names[idx] if idx < len(names) else "").strip()
        value = (values[idx] if idx < len(values) else "").strip()
        operator = (operators[idx] if idx < len(operators) else "").strip() or None
        if not name and not value:
            continue
        if not name or not value:
            return [], "Each RADIUS attribute row needs both an attribute and a value."
        attrs.append({"attribute": name, "operator": operator, "value": value})
    return attrs, None


def parse_profile_form(form) -> tuple[dict[str, object], list[dict], str | None]:
    name = form.get("name", "").strip()
    vendor = form.get("vendor", "").strip()
    description = form.get("description", "").strip()
    is_active = form.get("is_active") == "true"
    attributes, attr_error = parse_profile_attributes(form)

    profile_data = {
        "name": name,
        "description": description or None,
        "is_active": is_active,
    }
    if vendor:
        profile_data["vendor"] = vendor

    if not name:
        return profile_data, attributes, "Profile name is required."
    if attr_error:
        return profile_data, attributes, attr_error
    return profile_data, attributes, None


def create_profile(db, profile_data: dict[str, object], attributes: list[dict]):
    payload = RadiusProfileCreate.model_validate(profile_data)
    profile = catalog_service.radius_profiles.create(db=db, payload=payload)
    for attr in attributes:
        catalog_service.radius_attributes.create(
            db=db,
            payload=RadiusAttributeCreate(profile_id=profile.id, **attr),
        )
    metadata = {
        "name": profile.name,
        "vendor": profile.vendor.value if profile.vendor else None,
        "attributes": {"from": 0, "to": len(attributes)},
    }
    return profile, metadata


def update_profile(
    db,
    *,
    profile_id: str,
    profile_data: dict[str, object],
    attributes: list[dict],
):
    profile = catalog_service.radius_profiles.get(db=db, profile_id=profile_id)
    before_snapshot = model_to_dict(profile)
    existing = catalog_service.radius_attributes.list(
        db=db,
        profile_id=profile_id,
        order_by="attribute",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    before_attr_count = len(existing)

    payload = RadiusProfileUpdate.model_validate(profile_data)
    updated_profile = catalog_service.radius_profiles.update(
        db=db,
        profile_id=profile_id,
        payload=payload,
    )
    for attr in existing:
        catalog_service.radius_attributes.delete(db=db, attribute_id=str(attr.id))
    for attr in attributes:
        catalog_service.radius_attributes.create(
            db=db,
            payload=RadiusAttributeCreate(profile_id=profile.id, **attr),
        )

    after_snapshot = model_to_dict(updated_profile)
    changes = diff_dicts(before_snapshot, after_snapshot)
    after_attr_count = len(attributes)
    if before_attr_count != after_attr_count:
        changes["attributes"] = {"from": before_attr_count, "to": after_attr_count}
    metadata = {"changes": changes} if changes else None
    return updated_profile, metadata


def build_client_create_payload(values: dict[str, object]) -> RadiusClientCreate:
    server_id_raw = str(values.get("server_id") or "")
    client_ip = str(values.get("client_ip") or "")
    return RadiusClientCreate.model_validate(
        {
            "server_id": coerce_uuid(server_id_raw),
            "client_ip": client_ip,
            "shared_secret_hash": hashlib.sha256(
                str(values.get("shared_secret") or "").encode("utf-8")
            ).hexdigest(),
            "description": (str(values.get("description") or "") or None),
            "is_active": bool(values.get("is_active")),
        }
    )
