"""Service helpers for admin CPE management web routes."""

from __future__ import annotations

import logging
from datetime import datetime

from app.models.catalog import Subscription
from app.models.network import DeviceStatus, DeviceType
from app.models.subscriber import Address, Subscriber
from app.schemas.network import CPEDeviceCreate, CPEDeviceUpdate
from app.services import network as network_service
from app.services.common import coerce_uuid, validate_enum

logger = logging.getLogger(__name__)

_CPE_META_KEYS = ("winbox_host", "api_host", "api_port", "api_user")


def parse_cpe_notes_metadata(notes: str | None) -> tuple[dict[str, str | None], str | None]:
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
                metadata[key] = stripped[len(token):-1].strip() or None
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
        "subscription_id": str(form.get("subscription_id") or "").strip() or None,
        "service_address_id": str(form.get("service_address_id") or "").strip() or None,
        "device_type": str(form.get("device_type") or "").strip() or DeviceType.ont.value,
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


def cpe_form_snapshot(values: dict[str, object], *, cpe_id: str | None = None) -> dict[str, object]:
    data = dict(values)
    if cpe_id:
        data["id"] = cpe_id
    return data


def cpe_form_snapshot_from_model(cpe) -> dict[str, object]:
    meta, cleaned = parse_cpe_notes_metadata(getattr(cpe, "notes", None))
    return {
        "id": str(cpe.id),
        "subscriber_id": str(cpe.subscriber_id),
        "subscription_id": str(cpe.subscription_id) if cpe.subscription_id else "",
        "service_address_id": str(cpe.service_address_id) if cpe.service_address_id else "",
        "device_type": cpe.device_type.value,
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


def validate_cpe_values(values: dict[str, object]) -> str | None:
    if not values.get("subscriber_id"):
        return "Subscriber is required."
    return None


def create_cpe(db, values: dict[str, object]):
    normalized = dict(values)
    normalized["subscriber_id"] = coerce_uuid(str(values.get("subscriber_id") or ""))
    if values.get("subscription_id"):
        normalized["subscription_id"] = coerce_uuid(str(values.get("subscription_id")))
    if values.get("service_address_id"):
        normalized["service_address_id"] = coerce_uuid(str(values.get("service_address_id")))
    normalized["device_type"] = validate_enum(
        str(values.get("device_type") or DeviceType.ont.value), DeviceType, "device_type"
    )
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
        normalized["subscriber_id"] = coerce_uuid(str(values.get("subscriber_id")))
    if values.get("subscription_id"):
        normalized["subscription_id"] = coerce_uuid(str(values.get("subscription_id")))
    if values.get("service_address_id"):
        normalized["service_address_id"] = coerce_uuid(str(values.get("service_address_id")))
    normalized["device_type"] = validate_enum(
        str(values.get("device_type") or DeviceType.ont.value), DeviceType, "device_type"
    )
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


def cpe_form_reference_data(db, *, subscriber_id: str | None = None) -> dict[str, object]:
    subscribers = db.query(Subscriber).order_by(Subscriber.first_name.asc(), Subscriber.last_name.asc()).limit(500).all()
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
        "subscribers": subscribers,
        "subscriptions": subscriptions,
        "addresses": addresses,
        "device_types": [item.value for item in DeviceType],
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
        subscription_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
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
            if search_q in " ".join(
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
    subscribers = db.query(Subscriber).order_by(Subscriber.first_name.asc(), Subscriber.last_name.asc()).limit(500).all()
    return {
        "cpes": devices,
        "vendors": vendors,
        "subscribers": subscribers,
        "stats": {
            "total": len(devices),
            "active": sum(1 for d in devices if d.status.value == DeviceStatus.active.value),
            "mikrotik": sum(1 for d in devices if "mikrotik" in str(d.vendor or "").lower()),
        },
        "filters": {
            "search": str(search or "").strip(),
            "status": status_q,
            "vendor": str(vendor or "").strip(),
            "subscriber_id": str(subscriber_id or "").strip(),
        },
    }
