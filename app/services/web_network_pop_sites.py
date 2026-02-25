"""Service helpers for admin POP site web routes."""

from __future__ import annotations

import json
import logging
import uuid
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.catalog import NasDevice, Subscription
from app.models.network import NetworkZone
from app.models.network_monitoring import NetworkDevice, PopSite, PopSiteContact
from app.models.stored_file import StoredFile
from app.models.subscriber import Organization, Reseller
from app.models.wireless_mast import WirelessMast
from app.schemas.wireless_mast import WirelessMastCreate
from app.services import nas as nas_service
from app.services import wireless_mast as wireless_mast_service
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

DOCUMENT_CATEGORY_LABELS = {
    "lease": "Lease Agreement",
    "permit": "Permit",
    "survey": "Site Survey",
    "asbuilt": "As-Built Drawing",
    "other": "Other",
}


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value.strip() if isinstance(value, str) else default


def default_mast_context() -> dict[str, object]:
    """Return default mast form values."""
    return {"status": "active", "is_active": True, "metadata": ""}


def parse_mast_form(
    form: FormData,
    fallback_lat: float | None,
    fallback_lon: float | None,
) -> tuple[bool, dict[str, object] | None, str | None, dict[str, object]]:
    """Parse optional mast creation fields from form data."""
    mast_enabled = _form_str(form, "create_mast") == "true" or _form_str(form, "add_mast") == "true"
    mast_is_active_raw = form.get("mast_is_active")
    mast_defaults: dict[str, object] = {
        "name": _form_str(form, "mast_name"),
        "latitude": _form_str(form, "mast_latitude"),
        "longitude": _form_str(form, "mast_longitude"),
        "height_m": _form_str(form, "mast_height_m"),
        "structure_type": _form_str(form, "mast_structure_type"),
        "owner": _form_str(form, "mast_owner"),
        "status": _form_str(form, "mast_status") or "active",
        "notes": _form_str(form, "mast_notes"),
        "metadata": _form_str(form, "mast_metadata"),
        "is_active": str(mast_is_active_raw).strip().lower() in {"true", "on", "1", "yes"},
    }
    if not mast_enabled:
        return False, None, None, {**default_mast_context(), **mast_defaults}

    if not mast_defaults["name"]:
        return True, None, "Mast name is required when creating a mast.", mast_defaults

    def parse_float(value: str, label: str) -> tuple[float | None, str | None]:
        if not value:
            return None, None
        try:
            return float(value), None
        except ValueError:
            return None, f"{label} must be a valid number."

    lat, error = parse_float(str(mast_defaults["latitude"]), "Mast latitude")
    if error:
        return True, None, error, mast_defaults
    lon, error = parse_float(str(mast_defaults["longitude"]), "Mast longitude")
    if error:
        return True, None, error, mast_defaults
    if lat is None:
        lat = fallback_lat
    if lon is None:
        lon = fallback_lon
    if lat is None or lon is None:
        return True, None, "Mast latitude and longitude are required (or set POP site coordinates).", mast_defaults

    height_m, error = parse_float(str(mast_defaults["height_m"]), "Mast height")
    if error:
        return True, None, error, mast_defaults

    metadata = None
    if mast_defaults["metadata"]:
        try:
            metadata = json.loads(str(mast_defaults["metadata"]))
        except json.JSONDecodeError:
            return True, None, "Mast metadata must be valid JSON.", mast_defaults

    mast_data: dict[str, object] = {
        "name": mast_defaults["name"],
        "latitude": lat,
        "longitude": lon,
        "height_m": height_m,
        "structure_type": mast_defaults["structure_type"] or None,
        "owner": mast_defaults["owner"] or None,
        "status": mast_defaults["status"] or "active",
        "notes": mast_defaults["notes"] or None,
        "metadata_": metadata,
        "is_active": mast_defaults["is_active"],
    }
    return True, mast_data, None, {**default_mast_context(), **mast_defaults}


def parse_site_form_values(form: FormData) -> dict[str, object]:
    """Parse POP site form fields."""
    return {
        "name": _form_str(form, "name"),
        "code": (_form_str(form, "code") or None),
        "address_line1": (_form_str(form, "address_line1") or None),
        "address_line2": (_form_str(form, "address_line2") or None),
        "city": (_form_str(form, "city") or None),
        "region": (_form_str(form, "region") or None),
        "postal_code": (_form_str(form, "postal_code") or None),
        "country_code": (_form_str(form, "country_code") or None),
        "latitude_raw": _form_str(form, "latitude"),
        "longitude_raw": _form_str(form, "longitude"),
        "zone_id_raw": _form_str(form, "zone_id"),
        "organization_id_raw": _form_str(form, "organization_id"),
        "reseller_id_raw": _form_str(form, "reseller_id"),
        "notes": (_form_str(form, "notes") or None),
        "is_active": _form_str(form, "is_active") == "true",
    }


def _parse_optional_float(value: str, label: str) -> tuple[float | None, str | None]:
    if not value:
        return None, None
    try:
        return float(value), None
    except ValueError:
        return None, f"{label} must be a valid number."


def validate_site_values(values: dict[str, object]) -> tuple[dict[str, object] | None, str | None]:
    """Validate and normalize POP site values."""
    if not values.get("name"):
        return None, "Site name is required"
    latitude, error = _parse_optional_float(str(values.get("latitude_raw") or ""), "Latitude")
    if error:
        return None, error
    longitude, error = _parse_optional_float(str(values.get("longitude_raw") or ""), "Longitude")
    if error:
        return None, error
    normalized = dict(values)
    normalized.update({"latitude": latitude, "longitude": longitude})
    return normalized, None


def _parse_optional_uuid(value: str, label: str) -> tuple[uuid.UUID | None, str | None]:
    if not value:
        return None, None
    try:
        return uuid.UUID(value), None
    except ValueError:
        return None, f"{label} must be a valid identifier."


def resolve_site_relationships(
    db: Session,
    values: dict[str, object],
) -> tuple[dict[str, object] | None, str | None]:
    normalized = dict(values)
    zone_id, error = _parse_optional_uuid(str(values.get("zone_id_raw") or ""), "Location reference")
    if error:
        return None, error
    organization_id, error = _parse_optional_uuid(str(values.get("organization_id_raw") or ""), "Organization")
    if error:
        return None, error
    reseller_id, error = _parse_optional_uuid(str(values.get("reseller_id_raw") or ""), "Partner")
    if error:
        return None, error

    if zone_id and not db.get(NetworkZone, zone_id):
        return None, "Selected location reference was not found."
    if organization_id and not db.get(Organization, organization_id):
        return None, "Selected organization was not found."
    if reseller_id and not db.get(Reseller, reseller_id):
        return None, "Selected partner was not found."

    normalized["zone_id"] = zone_id
    normalized["organization_id"] = organization_id
    normalized["reseller_id"] = reseller_id
    return normalized, None


def form_reference_data(db: Session) -> dict[str, object]:
    return {
        "zones": db.scalars(select(NetworkZone).where(NetworkZone.is_active.is_(True)).order_by(NetworkZone.name)).all(),
        "organizations": db.scalars(select(Organization).order_by(Organization.name)).all(),
        "resellers": db.scalars(select(Reseller).where(Reseller.is_active.is_(True)).order_by(Reseller.name)).all(),
    }


def build_form_context(
    *,
    pop_site: PopSite | dict[str, object] | None,
    action_url: str,
    error: str | None = None,
    mast_error: str | None = None,
    mast_enabled: bool = False,
    mast_defaults: dict[str, object] | None = None,
    reference_data: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build shared context for POP site form page."""
    context: dict[str, object] = {
        "pop_site": pop_site,
        "action_url": action_url,
        "mast_enabled": mast_enabled,
        "mast_defaults": mast_defaults or default_mast_context(),
    }
    if reference_data:
        context.update(reference_data)
    if error:
        context["error"] = error
    if mast_error:
        context["mast_error"] = mast_error
    return context


def create_site(db: Session, values: dict[str, object]) -> PopSite:
    """Create and persist POP site."""
    pop_site = PopSite(
        name=values["name"],
        code=values.get("code"),
        address_line1=values.get("address_line1"),
        address_line2=values.get("address_line2"),
        city=values.get("city"),
        region=values.get("region"),
        postal_code=values.get("postal_code"),
        country_code=values.get("country_code"),
        latitude=values.get("latitude"),
        longitude=values.get("longitude"),
        zone_id=values.get("zone_id"),
        organization_id=values.get("organization_id"),
        reseller_id=values.get("reseller_id"),
        notes=values.get("notes"),
        is_active=bool(values.get("is_active")),
    )
    db.add(pop_site)
    db.commit()
    db.refresh(pop_site)
    return pop_site


def apply_site_update(pop_site: PopSite, values: dict[str, object]) -> None:
    """Apply validated values to POP site model."""
    pop_site.name = cast(str, values["name"])
    pop_site.code = cast(str | None, values.get("code"))
    pop_site.address_line1 = cast(str | None, values.get("address_line1"))
    pop_site.address_line2 = cast(str | None, values.get("address_line2"))
    pop_site.city = cast(str | None, values.get("city"))
    pop_site.region = cast(str | None, values.get("region"))
    pop_site.postal_code = cast(str | None, values.get("postal_code"))
    pop_site.country_code = cast(str | None, values.get("country_code"))
    pop_site.latitude = cast(float | None, values.get("latitude"))
    pop_site.longitude = cast(float | None, values.get("longitude"))
    pop_site.zone_id = cast(uuid.UUID | None, values.get("zone_id"))
    pop_site.organization_id = cast(uuid.UUID | None, values.get("organization_id"))
    pop_site.reseller_id = cast(uuid.UUID | None, values.get("reseller_id"))
    pop_site.notes = cast(str | None, values.get("notes"))
    pop_site.is_active = bool(values.get("is_active"))


def commit_site_update(db: Session, pop_site: PopSite, values: dict[str, object]) -> None:
    """Apply values and commit POP site update."""
    apply_site_update(pop_site, values)
    db.flush()


def get_pop_site(db: Session, pop_site_id: str) -> PopSite | None:
    """Get POP site by id."""
    return db.scalars(select(PopSite).where(PopSite.id == pop_site_id)).first()


def list_page_data(db: Session, status: str | None) -> dict[str, object]:
    """Return list payload for POP site index."""
    stmt = select(PopSite).order_by(PopSite.name)
    status_filter = (status or "all").strip().lower()
    if status_filter == "active":
        stmt = stmt.where(PopSite.is_active.is_(True))
    elif status_filter == "inactive":
        stmt = stmt.where(PopSite.is_active.is_(False))
    pop_sites = db.scalars(stmt.limit(100)).all()
    all_sites = db.scalars(select(PopSite)).all()
    return {
        "pop_sites": pop_sites,
        "stats": {
            "total": len(all_sites),
            "active": sum(1 for p in all_sites if p.is_active),
            "inactive": sum(1 for p in all_sites if not p.is_active),
        },
        "status_filter": status_filter,
    }


def detail_page_data(db: Session, pop_site_id: str) -> dict[str, object] | None:
    """Return POP site detail payload."""
    pop_site = get_pop_site(db, pop_site_id)
    if not pop_site:
        return None
    devices = db.scalars(
        select(NetworkDevice)
        .where(NetworkDevice.pop_site_id == pop_site.id)
        .order_by(NetworkDevice.name)
    ).all()
    nas_devices = db.scalars(
        select(NasDevice)
        .where(NasDevice.pop_site_id == pop_site.id)
        .where(NasDevice.is_active.is_(True))
        .order_by(NasDevice.name)
    ).all()
    masts = db.scalars(
        select(WirelessMast)
        .where(WirelessMast.pop_site_id == pop_site.id)
        .order_by(WirelessMast.name)
    ).all()
    subscriptions = db.scalars(
        select(Subscription)
        .join(NasDevice, Subscription.provisioning_nas_device_id == NasDevice.id)
        .where(NasDevice.pop_site_id == pop_site.id)
        .where(NasDevice.is_active.is_(True))
        .order_by(Subscription.updated_at.desc())
    ).all()

    hardware_devices: list[dict[str, object]] = []
    for device in devices:
        hardware_devices.append(
            {
                "id": str(device.id),
                "name": device.name,
                "device_type": "Core Device",
                "role": device.role.value.title() if device.role else "-",
                "ip": device.mgmt_ip or device.hostname or "-",
                "status": device.status.value.title() if device.status else "Unknown",
                "ping_status": "OK" if device.last_ping_ok else "Timeout",
                "snmp_status": "OK" if device.last_snmp_ok else "Unknown",
                "detail_url": f"/admin/network/core-devices/{device.id}",
            }
        )
    for nas in nas_devices:
        ping = nas_service.get_ping_status(nas.ip_address or nas.management_ip)
        ping_ok = ping.get("state") == "reachable"
        hardware_devices.append(
            {
                "id": str(nas.id),
                "name": nas.name,
                "device_type": "NAS Router",
                "role": "NAS",
                "ip": nas.ip_address or nas.management_ip or "-",
                "status": nas.status.value.title() if nas.status else "Unknown",
                "ping_status": "OK" if ping_ok else "Timeout",
                "snmp_status": "Configured" if nas.snmp_community else "Unknown",
                "detail_url": f"/admin/network/nas/devices/{nas.id}",
            }
        )

    customer_services: list[dict[str, object]] = []
    for subscription in subscriptions:
        subscriber = subscription.subscriber
        service_address = subscription.service_address
        customer_services.append(
            {
                "subscription_id": str(subscription.id),
                "subscriber_id": str(subscriber.id) if subscriber else None,
                "subscriber_name": (
                    f"{subscriber.first_name} {subscriber.last_name}".strip()
                    if subscriber
                    else "Unknown"
                ),
                "subscriber_number": subscriber.subscriber_number if subscriber else None,
                "subscription_status": subscription.status.value.title()
                if subscription.status
                else "Unknown",
                "service_description": subscription.service_description
                or (subscription.offer.name if subscription.offer else "-"),
                "login": subscription.login or "-",
                "ipv4_address": subscription.ipv4_address or "-",
                "nas_name": subscription.provisioning_nas_device.name
                if subscription.provisioning_nas_device
                else "-",
                "latitude": service_address.latitude if service_address else None,
                "longitude": service_address.longitude if service_address else None,
                "address_label": service_address.label if service_address else None,
            }
        )

    map_markers: list[dict[str, object]] = []
    for service in customer_services:
        lat = service.get("latitude")
        lon = service.get("longitude")
        if lat is None or lon is None:
            continue
        map_markers.append(
            {
                "type": "service",
                "latitude": lat,
                "longitude": lon,
                "title": service.get("subscriber_name"),
                "subtitle": service.get("service_description"),
                "meta": service.get("address_label") or service.get("ipv4_address"),
            }
        )
    for mast in masts:
        if mast.latitude is None or mast.longitude is None:
            continue
        map_markers.append(
            {
                "type": "hardware",
                "latitude": mast.latitude,
                "longitude": mast.longitude,
                "title": mast.name,
                "subtitle": "Wireless Mast",
                "meta": mast.status or "",
            }
        )

    photo_files = (
        db.query(StoredFile)
        .filter(StoredFile.entity_type == "pop_site_photo")
        .filter(StoredFile.entity_id == str(pop_site.id))
        .filter(StoredFile.is_deleted.is_(False))
        .order_by(StoredFile.created_at.desc())
        .all()
    )
    contacts = (
        db.query(PopSiteContact)
        .filter(PopSiteContact.pop_site_id == pop_site.id)
        .filter(PopSiteContact.is_active.is_(True))
        .order_by(PopSiteContact.is_primary.desc(), PopSiteContact.created_at.desc())
        .all()
    )
    document_records = (
        db.query(StoredFile)
        .filter(StoredFile.entity_type.like("pop_site_document_%"))
        .filter(StoredFile.entity_id == str(pop_site.id))
        .filter(StoredFile.is_deleted.is_(False))
        .order_by(StoredFile.created_at.desc())
        .all()
    )
    documents: list[dict[str, object]] = []
    for doc in document_records:
        category = "other"
        if doc.entity_type.startswith("pop_site_document_"):
            category = doc.entity_type.replace("pop_site_document_", "", 1) or "other"
        documents.append(
            {
                "id": doc.id,
                "filename": doc.original_filename,
                "file_size": doc.file_size,
                "content_type": doc.content_type,
                "created_at": doc.created_at,
                "category": category,
                "category_label": DOCUMENT_CATEGORY_LABELS.get(category, "Other"),
                "uploaded_by": doc.uploaded_by,
            }
        )

    return {
        "pop_site": pop_site,
        "devices": devices,
        "nas_devices": nas_devices,
        "hardware_devices": hardware_devices,
        "masts": masts,
        "customer_services": customer_services,
        "service_impact_count": len(customer_services),
        "map_markers": map_markers,
        "photo_files": photo_files,
        "documents": documents,
        "contacts": contacts,
    }


def get_site_file_or_none(db: Session, file_id: str) -> StoredFile | None:
    try:
        file_uuid = uuid.UUID(file_id)
    except ValueError:
        return None
    record = db.get(StoredFile, file_uuid)
    if not record or record.is_deleted:
        return None
    if record.entity_type != "pop_site_photo" and not record.entity_type.startswith("pop_site_document_"):
        return None
    return record


def create_contact(
    db: Session,
    *,
    pop_site_id: str,
    name: str,
    role: str | None,
    phone: str | None,
    email: str | None,
    notes: str | None,
    is_primary: bool,
) -> PopSiteContact:
    if is_primary:
        db.query(PopSiteContact).filter(
            PopSiteContact.pop_site_id == coerce_uuid(pop_site_id),
            PopSiteContact.is_active.is_(True),
            PopSiteContact.is_primary.is_(True),
        ).update({"is_primary": False}, synchronize_session=False)
    contact = PopSiteContact(
        pop_site_id=coerce_uuid(pop_site_id),
        name=name,
        role=role,
        phone=phone,
        email=email,
        notes=notes,
        is_primary=is_primary,
        is_active=True,
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


def delete_contact(db: Session, *, pop_site_id: str, contact_id: str) -> bool:
    try:
        contact_uuid = coerce_uuid(contact_id)
    except Exception:
        return False
    contact = db.get(PopSiteContact, contact_uuid)
    if not contact or str(contact.pop_site_id) != str(pop_site_id):
        return False
    contact.is_active = False
    contact.is_primary = False
    db.add(contact)
    db.commit()
    return True


def maybe_create_mast(db: Session, pop_site_id: str, mast_data: dict[str, object] | None) -> None:
    """Create a wireless mast if payload is provided."""
    if not mast_data:
        return
    mast_payload = WirelessMastCreate.model_validate(
        {**mast_data, "pop_site_id": coerce_uuid(pop_site_id)}
    )
    wireless_mast_service.wireless_masts.create(db, mast_payload)
