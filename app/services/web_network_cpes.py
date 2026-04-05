"""Service helpers for admin CPE management web routes."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.network import CPEDevice, DeviceStatus, OntAssignment, OntUnit
from app.models.subscriber import Address, Subscriber, UserType
from app.models.tr069 import Tr069CpeDevice
from app.schemas.network import CPEDeviceCreate, CPEDeviceUpdate
from app.services import network as network_service
from app.services.network import cpe as cpe_service
from app.services.common import coerce_uuid, validate_enum
from app.services.network._common import decode_huawei_hex_serial, normalize_mac_address

logger = logging.getLogger(__name__)

_CPE_META_KEYS = ("winbox_host", "api_host", "api_port", "api_user")
_CPE_DEVICE_TYPE_OPTIONS = [
    "router",
    "switch",
    "hub",
    "firewall",
    "inverter",
    "access_point",
    "bridge",
    "modem",
    "server",
    "other",
]
_CPE_DEFAULT_DEVICE_TYPE = "router"


def _vendor_from_serial(value: str | None) -> str | None:
    serial = str(value or "").strip().upper()
    decoded = decode_huawei_hex_serial(serial)
    probe = decoded or serial
    if probe.startswith(("HWT", "HW")):
        return "Huawei"
    if probe.startswith("ZT"):
        return "ZTE"
    if probe.startswith("NK"):
        return "Nokia"
    return None


def resolve_authoritative_cpe_mac(db: Session, cpe: CPEDevice) -> str | None:
    """Return the best MAC for a CPE, preferring linked active ONT inventory.

    CPE devices link directly to subscribers (not subscriptions), enabling
    independent OLT management.
    """
    serial_number = str(getattr(cpe, "serial_number", "") or "").strip()
    subscriber_id = getattr(cpe, "subscriber_id", None)

    # First try by serial number match
    if serial_number:
        ont = db.scalars(
            select(OntUnit)
            .where(OntUnit.serial_number == serial_number)
            .order_by(OntUnit.updated_at.desc(), OntUnit.created_at.desc())
            .limit(1)
        ).first()
        ont_mac = normalize_mac_address(getattr(ont, "mac_address", None))
        if ont_mac:
            return ont_mac

    # Then try by subscriber's active ONT assignment
    if subscriber_id is not None:
        ont = db.scalars(
            select(OntUnit)
            .join(OntAssignment, OntAssignment.ont_unit_id == OntUnit.id)
            .where(OntAssignment.subscriber_id == subscriber_id)
            .where(OntAssignment.active.is_(True))
            .order_by(OntAssignment.updated_at.desc(), OntAssignment.created_at.desc())
            .limit(1)
        ).first()
        ont_mac = normalize_mac_address(getattr(ont, "mac_address", None))
        if ont_mac:
            return ont_mac

    return normalize_mac_address(getattr(cpe, "mac_address", None))


def build_cpe_identity_context(db: Session, cpe: CPEDevice) -> dict[str, object]:
    linked_tr069 = (
        db.query(Tr069CpeDevice)
        .filter(Tr069CpeDevice.cpe_device_id == cpe.id)
        .filter(Tr069CpeDevice.is_active.is_(True))
        .order_by(Tr069CpeDevice.updated_at.desc(), Tr069CpeDevice.created_at.desc())
        .first()
    )
    raw_serial = str(
        cpe.serial_number or (linked_tr069.serial_number if linked_tr069 else "") or ""
    ).strip()
    decoded_serial = decode_huawei_hex_serial(raw_serial)
    display_serial = decoded_serial or raw_serial or None
    vendor = (
        str(cpe.vendor or "").strip()
        or str(_vendor_from_serial(raw_serial) or "")
        or ""
    ).strip() or None
    model = (
        str(cpe.model or "").strip()
        or str(getattr(linked_tr069, "product_class", "") or "").strip()
        or None
    )
    mac_address = resolve_authoritative_cpe_mac(db, cpe)
    return {
        "linked_tr069": linked_tr069,
        "display_serial": display_serial,
        "raw_serial": raw_serial or None,
        "vendor": vendor,
        "model": model,
        "mac_address": mac_address,
        "oui": str(getattr(linked_tr069, "oui", "") or "").strip() or None,
        "product_class": str(getattr(linked_tr069, "product_class", "") or "").strip()
        or None,
        "connection_request_url": str(
            getattr(linked_tr069, "connection_request_url", "") or ""
        ).strip()
        or None,
        "last_inform_at": getattr(linked_tr069, "last_inform_at", None),
    }


def _normalize_cpe_device_type(value: str | None) -> str:
    device_type = str(value or "").strip()
    if device_type in _CPE_DEVICE_TYPE_OPTIONS:
        return device_type
    if device_type in {"cpe", "ont"}:
        return _CPE_DEFAULT_DEVICE_TYPE
    return _CPE_DEFAULT_DEVICE_TYPE


def parse_cpe_notes_metadata(
    notes: str | None,
) -> tuple[dict[str, str | None], str | None]:
    text = str(notes or "").strip()
    metadata = dict.fromkeys(_CPE_META_KEYS)
    if not text:
        return metadata, None
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        matched = False
        for key in _CPE_META_KEYS:
            token = f"[{key}:"
            if lowered.startswith(token) and lowered.endswith("]"):
                metadata[key] = stripped[len(token) : -1].strip() or None
                matched = True
                break
        if not matched:
            cleaned_lines.append(stripped)
    cleaned = "\n".join(line for line in cleaned_lines if line).strip() or None
    return metadata, cleaned


def normalize_cpe_notes(
    *,
    notes: str | None,
    winbox_host: str | None,
    api_host: str | None,
    api_port: str | None,
    api_user: str | None,
) -> str | None:
    _, cleaned = parse_cpe_notes_metadata(notes)
    metadata = {
        "winbox_host": str(winbox_host or "").strip() or None,
        "api_host": str(api_host or "").strip() or None,
        "api_port": str(api_port or "").strip() or None,
        "api_user": str(api_user or "").strip() or None,
    }
    lines: list[str] = []
    for key in _CPE_META_KEYS:
        value = metadata.get(key)
        if value:
            lines.append(f"[{key}:{value}]")
    if cleaned:
        lines.append(cleaned)
    return "\n".join(lines).strip() or None


def parse_cpe_form(form) -> dict[str, object]:
    installed_raw = str(form.get("installed_at") or "").strip()
    installed_at = None
    if installed_raw:
        try:
            installed_at = datetime.fromisoformat(installed_raw)
        except ValueError:
            installed_at = None
    return {
        "subscriber_id": str(form.get("subscriber_id") or "").strip(),
        "service_address_id": str(form.get("service_address_id") or "").strip() or None,
        "device_type": _normalize_cpe_device_type(form.get("device_type")),
        "status": str(form.get("status") or "").strip() or DeviceStatus.active.value,
        "serial_number": str(form.get("serial_number") or "").strip() or None,
        "model": str(form.get("model") or "").strip() or None,
        "vendor": str(form.get("vendor") or "").strip() or None,
        "mac_address": str(form.get("mac_address") or "").strip() or None,
        "installed_at": installed_at,
        "notes": str(form.get("notes") or "").strip() or None,
        "winbox_host": str(form.get("winbox_host") or "").strip() or None,
        "api_host": str(form.get("api_host") or "").strip() or None,
        "api_port": str(form.get("api_port") or "").strip() or None,
        "api_user": str(form.get("api_user") or "").strip() or None,
    }


def cpe_form_snapshot(
    values: dict[str, object], *, cpe_id: str | None = None
) -> dict[str, object]:
    data = dict(values)
    data["device_type"] = _normalize_cpe_device_type(data.get("device_type"))
    if cpe_id:
        data["id"] = cpe_id
    return data


def cpe_form_snapshot_from_model(cpe) -> dict[str, object]:
    meta, cleaned = parse_cpe_notes_metadata(getattr(cpe, "notes", None))
    return {
        "id": str(cpe.id),
        "subscriber_id": str(cpe.subscriber_id),
        "service_address_id": str(cpe.service_address_id)
        if cpe.service_address_id
        else "",
        "device_type": _normalize_cpe_device_type(cpe.device_type.value),
        "status": cpe.status.value,
        "serial_number": cpe.serial_number or "",
        "model": cpe.model or "",
        "vendor": cpe.vendor or "",
        "mac_address": cpe.mac_address or "",
        "installed_at": cpe.installed_at,
        "notes": cleaned or "",
        "winbox_host": meta.get("winbox_host") or "",
        "api_host": meta.get("api_host") or "",
        "api_port": meta.get("api_port") or "",
        "api_user": meta.get("api_user") or "",
    }


def _resolve_subscriber_label(db: Session, subscriber_id: str | None) -> str:
    """Return a typeahead-friendly subscriber label for the CPE form."""
    selected_subscriber_id = str(subscriber_id or "").strip()
    if not selected_subscriber_id:
        return ""
    try:
        subscriber = (
            db.query(Subscriber)
            .filter(Subscriber.id == coerce_uuid(selected_subscriber_id))
            .first()
        )
    except Exception:
        logger.warning(
            "Failed to resolve CPE subscriber label for %s",
            selected_subscriber_id,
            exc_info=True,
        )
        return ""
    if not subscriber:
        return ""
    label = str(getattr(subscriber, "name", "") or "").strip() or "Subscriber"
    if subscriber.account_number:
        label = f"{label} ({subscriber.account_number})"
    elif subscriber.subscriber_number:
        label = f"{label} ({subscriber.subscriber_number})"
    return label


def validate_cpe_values(values: dict[str, object]) -> str | None:
    if not values.get("subscriber_id"):
        return "Subscriber is required."
    return None


def create_cpe(db, values: dict[str, object]):
    normalized = dict(values)
    normalized["account_id"] = coerce_uuid(str(values.get("subscriber_id") or ""))
    normalized.pop("subscriber_id", None)
    if values.get("service_address_id"):
        normalized["service_address_id"] = coerce_uuid(
            str(values.get("service_address_id"))
        )
    normalized["device_type"] = _normalize_cpe_device_type(values.get("device_type"))
    normalized["status"] = validate_enum(
        str(values.get("status") or DeviceStatus.active.value), DeviceStatus, "status"
    )
    normalized["notes"] = normalize_cpe_notes(
        notes=values.get("notes"),
        winbox_host=values.get("winbox_host"),
        api_host=values.get("api_host"),
        api_port=values.get("api_port"),
        api_user=values.get("api_user"),
    )
    normalized.pop("winbox_host", None)
    normalized.pop("api_host", None)
    normalized.pop("api_port", None)
    normalized.pop("api_user", None)
    payload = CPEDeviceCreate.model_validate(normalized)
    return network_service.cpe_devices.create(db=db, payload=payload)


def update_cpe(db, *, cpe_id: str, values: dict[str, object]):
    normalized = dict(values)
    if values.get("subscriber_id"):
        normalized["account_id"] = coerce_uuid(str(values.get("subscriber_id")))
    normalized.pop("subscriber_id", None)
    if values.get("service_address_id"):
        normalized["service_address_id"] = coerce_uuid(
            str(values.get("service_address_id"))
        )
    normalized["device_type"] = _normalize_cpe_device_type(values.get("device_type"))
    normalized["status"] = validate_enum(
        str(values.get("status") or DeviceStatus.active.value), DeviceStatus, "status"
    )
    normalized["notes"] = normalize_cpe_notes(
        notes=values.get("notes"),
        winbox_host=values.get("winbox_host"),
        api_host=values.get("api_host"),
        api_port=values.get("api_port"),
        api_user=values.get("api_user"),
    )
    normalized.pop("winbox_host", None)
    normalized.pop("api_host", None)
    normalized.pop("api_port", None)
    normalized.pop("api_user", None)
    payload = CPEDeviceUpdate.model_validate(normalized)
    return network_service.cpe_devices.update(db=db, device_id=cpe_id, payload=payload)


def get_cpe(db, *, cpe_id: str):
    return network_service.cpe_devices.get(db=db, device_id=cpe_id)


def cpe_form_reference_data(
    db, *, subscriber_id: str | None = None
) -> dict[str, object]:
    selected_subscriber_id = str(subscriber_id or "").strip()
    subscriptions: list[Subscription] = []
    addresses: list[Address] = []
    if selected_subscriber_id:
        subscriptions = (
            db.query(Subscription)
            .filter(Subscription.subscriber_id == coerce_uuid(selected_subscriber_id))
            .order_by(Subscription.created_at.desc())
            .limit(200)
            .all()
        )
        addresses = (
            db.query(Address)
            .filter(Address.subscriber_id == coerce_uuid(selected_subscriber_id))
            .order_by(Address.created_at.desc())
            .limit(200)
            .all()
        )
    return {
        "selected_subscriber_label": _resolve_subscriber_label(
            db, selected_subscriber_id
        ),
        "subscriptions": subscriptions,
        "addresses": addresses,
        "device_types": _CPE_DEVICE_TYPE_OPTIONS,
        "statuses": [item.value for item in DeviceStatus],
    }


def build_cpe_list_data(
    db,
    *,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    subscriber_id: str | None = None,
) -> dict[str, object]:
    subscriber_filter = str(subscriber_id or "").strip() or None
    devices = network_service.cpe_devices.list(
        db=db,
        subscriber_id=subscriber_filter,
        order_by="created_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
    inventory_subscriber_id = cpe_service.get_inventory_subscriber_id(db)
    if subscriber_filter is None and inventory_subscriber_id is not None:
        devices = [
            device
            for device in devices
            if getattr(device, "subscriber_id", None) != inventory_subscriber_id
        ]
    search_q = str(search or "").strip().lower()
    status_q = str(status or "").strip().lower()
    vendor_q = str(vendor or "").strip().lower()
    if status_q:
        devices = [d for d in devices if d.status.value == status_q]
    if vendor_q:
        devices = [d for d in devices if vendor_q in str(d.vendor or "").lower()]
    if search_q:
        devices = [
            d
            for d in devices
            if search_q
            in " ".join(
                [
                    str(d.serial_number or ""),
                    str(d.vendor or ""),
                    str(d.model or ""),
                    str(d.mac_address or ""),
                    str(d.subscriber.full_name if d.subscriber else ""),
                    str(d.subscriber.account_number if d.subscriber else ""),
                ]
            ).lower()
        ]
    vendors = sorted({str(d.vendor) for d in devices if d.vendor})
    subscribers = (
        db.query(Subscriber)
        .filter(Subscriber.user_type != UserType.system_user)
        .order_by(Subscriber.first_name.asc(), Subscriber.last_name.asc())
        .limit(500)
        .all()
    )
    return {
        "cpes": devices,
        "vendors": vendors,
        "subscribers": subscribers,
        "stats": {
            "total": len(devices),
            "active": sum(
                1 for d in devices if d.status.value == DeviceStatus.active.value
            ),
            "mikrotik": sum(
                1 for d in devices if "mikrotik" in str(d.vendor or "").lower()
            ),
        },
        "filters": {
            "search": str(search or "").strip(),
            "status": status_q,
            "vendor": str(vendor or "").strip(),
            "subscriber_id": str(subscriber_id or "").strip(),
        },
    }
