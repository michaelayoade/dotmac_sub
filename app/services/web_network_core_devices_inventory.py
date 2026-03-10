"""Inventory and list/filter helpers for network devices pages."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.models.network_monitoring import NetworkDevice
from app.services import network as network_service

if TYPE_CHECKING:
    from app.models.network import Port

logger = logging.getLogger(__name__)


def _network_device_is_olt_candidate(device: NetworkDevice) -> bool:
    """Treat monitoring devices with names ending in OLT as OLT list members."""
    return device.name.strip().lower().endswith("olt")


def resolve_olt_device_for_network_device(db: Session, device: NetworkDevice) -> OLTDevice:
    """Return a dedicated OLTDevice for a promoted NetworkDevice, creating one when missing."""
    if device.mgmt_ip:
        matched = db.scalars(select(OLTDevice).where(OLTDevice.mgmt_ip == device.mgmt_ip)).first()
        if matched:
            return matched
    if device.hostname:
        matched = db.scalars(select(OLTDevice).where(OLTDevice.hostname == device.hostname)).first()
        if matched:
            return matched

    matched = db.scalars(select(OLTDevice).where(OLTDevice.name == device.name)).first()
    if matched:
        return matched

    payload = {
        "name": device.name,
        "hostname": device.hostname,
        "mgmt_ip": device.mgmt_ip,
        "vendor": device.vendor,
        "model": device.model,
        "serial_number": device.serial_number,
        "notes": device.notes,
        "is_active": bool(device.is_active),
    }
    try:
        return network_service.olt_devices.create(db=db, payload=payload)
    except IntegrityError:
        db.rollback()
        if device.mgmt_ip:
            matched = db.scalars(select(OLTDevice).where(OLTDevice.mgmt_ip == device.mgmt_ip)).first()
            if matched:
                return matched
        if device.hostname:
            matched = db.scalars(select(OLTDevice).where(OLTDevice.hostname == device.hostname)).first()
            if matched:
                return matched
        matched = db.scalars(select(OLTDevice).where(OLTDevice.name == device.name)).first()
        if matched:
            return matched
        raise


def _status_presenter(raw_status: str | None) -> tuple[str, str]:
    status = (raw_status or "").strip().lower()
    if status == "online":
        return "Online", "online"
    if status == "offline":
        return "Offline", "offline"
    if status == "degraded":
        return "Degraded", "warning"
    if status:
        return status.replace("_", " ").title(), "default"
    return "Unknown", "default"


def _find_linked_monitoring_status(
    *,
    olt: OLTDevice,
    by_mgmt_ip: dict[str, NetworkDevice],
    by_hostname: dict[str, NetworkDevice],
    by_name: dict[str, NetworkDevice],
) -> tuple[str, str]:
    linked: NetworkDevice | None = None
    if olt.mgmt_ip:
        linked = by_mgmt_ip.get(olt.mgmt_ip)
    if linked is None and olt.hostname:
        linked = by_hostname.get(olt.hostname)
    if linked is None and olt.name:
        linked = by_name.get(olt.name)
    status_raw = linked.status.value if (linked and linked.status) else None
    return _status_presenter(status_raw)


def get_cpe_ports(db: Session, cpe_id: object) -> list[Port]:
    """Return ports for a CPE device."""
    from sqlalchemy import select

    from app.models.network import Port

    return list(db.scalars(select(Port).where(Port.device_id == cpe_id)).all())


def collect_devices(db: Session) -> list[dict]:
    """Collect all device types into a unified list of dicts."""
    devices: list[dict] = []

    olts = network_service.olt_devices.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=500, offset=0
    )
    for olt in olts:
        devices.append(
            {
                "id": str(olt.id),
                "name": olt.name,
                "type": "olt",
                "serial_number": getattr(olt, "serial_number", None),
                "ip_address": getattr(olt, "mgmt_ip", None),
                "vendor": olt.vendor,
                "model": olt.model,
                "status": "online" if olt.is_active else "offline",
                "last_seen": getattr(olt, "last_seen", None),
                "subscriber": None,
            }
        )

    onts = network_service.ont_units.list(
        db=db,
        is_active=True,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    for ont in onts:
        devices.append(
            {
                "id": str(ont.id),
                "name": getattr(ont, "name", None) or ont.serial_number,
                "type": "ont",
                "serial_number": ont.serial_number,
                "ip_address": getattr(ont, "ip_address", None),
                "vendor": ont.vendor,
                "model": ont.model,
                "status": "online" if ont.is_active else "offline",
                "last_seen": getattr(ont, "last_seen", None),
                "subscriber": None,
            }
        )

    cpes = network_service.cpe_devices.list(
        db=db,
        subscriber_id=None,
        subscription_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    for cpe in cpes:
        devices.append(
            {
                "id": str(cpe.id),
                "name": getattr(cpe, "name", None)
                or getattr(cpe, "serial_number", str(cpe.id)[:8]),
                "type": "cpe",
                "serial_number": getattr(cpe, "serial_number", None),
                "ip_address": getattr(cpe, "ip_address", None),
                "vendor": getattr(cpe, "vendor", None),
                "model": getattr(cpe, "model", None),
                "status": "online",
                "last_seen": getattr(cpe, "last_seen", None),
                "subscriber": None,
            }
        )

    return devices


def _device_matches_search(device: dict, term: str) -> bool:
    """Check if any device field matches the search term."""
    haystack = [
        device.get("name"),
        device.get("serial_number"),
        device.get("ip_address"),
        device.get("vendor"),
        device.get("model"),
        device.get("type"),
    ]
    return any((value or "").lower().find(term) != -1 for value in haystack)


def filter_devices(
    devices: list[dict],
    *,
    device_type: str | None = None,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
) -> list[dict]:
    """Apply optional filters to a device list."""
    if device_type and device_type != "all":
        devices = [d for d in devices if d["type"] == device_type]

    term = (search or "").strip().lower()
    if term:
        devices = [d for d in devices if _device_matches_search(d, term)]

    status_filter = (status or "").strip().lower()
    if status_filter:
        devices = [
            d for d in devices if (d.get("status") or "").lower() == status_filter
        ]

    vendor_filter = (vendor or "").strip().lower()
    if vendor_filter:
        devices = [
            d for d in devices if (d.get("vendor") or "").lower() == vendor_filter
        ]

    return devices


def compute_device_stats(devices: list[dict]) -> dict[str, int]:
    """Compute summary stats for a filtered device list."""
    return {
        "total": len(devices),
        "olt": sum(1 for d in devices if d["type"] == "olt"),
        "ont": sum(1 for d in devices if d["type"] == "ont"),
        "cpe": sum(1 for d in devices if d["type"] == "cpe"),
        "online": sum(1 for d in devices if d["status"] == "online"),
        "offline": sum(1 for d in devices if d["status"] == "offline"),
        "warning": 0,
        "unprovisioned": 0,
    }


def devices_list_page_data(
    db: Session,
    *,
    device_type: str | None = None,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
) -> dict[str, object]:
    """Return full payload for the devices index page."""
    devices = collect_devices(db)
    devices = filter_devices(
        devices, device_type=device_type, search=search, status=status, vendor=vendor
    )
    stats = compute_device_stats(devices)
    return {
        "devices": devices,
        "stats": stats,
        "device_type": device_type,
        "search": search or "",
        "status": status or "",
        "vendor": vendor or "",
    }


def devices_search_data(db: Session, search: str) -> list[dict]:
    """Return filtered devices for HTMX search partial."""
    devices = collect_devices(db)
    term = search.strip().lower()
    if term:
        devices = [d for d in devices if _device_matches_search(d, term)]
    return devices


def devices_filter_data(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
) -> list[dict]:
    """Return filtered devices for HTMX filter partial."""
    devices = collect_devices(db)
    return filter_devices(devices, search=search, status=status, vendor=vendor)


def olts_list_page_data(db: Session) -> dict[str, object]:
    """Return OLT list payload with per-OLT stats."""
    raw_olts = network_service.olt_devices.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    olt_stats = {}
    for olt in raw_olts:
        pon_ports = network_service.pon_ports.list(
            db=db,
            olt_id=str(olt.id),
            is_active=True,
            order_by="created_at",
            order_dir="asc",
            limit=1000,
            offset=0,
        )
        olt_stats[str(olt.id)] = {"pon_ports": len(pon_ports)}

    promoted_olts = [
        device
        for device in db.scalars(select(NetworkDevice).order_by(NetworkDevice.name)).all()
        if device.is_active and _network_device_is_olt_candidate(device)
    ]
    promoted_olt_records = [resolve_olt_device_for_network_device(db, device) for device in promoted_olts]
    all_olts_by_id = {str(olt.id): olt for olt in raw_olts}
    for olt in promoted_olt_records:
        all_olts_by_id[str(olt.id)] = olt

    all_olts = sorted(all_olts_by_id.values(), key=lambda olt: str(olt.name or "").lower())
    monitoring_devices = list(db.scalars(select(NetworkDevice)).all())
    by_mgmt_ip = {d.mgmt_ip: d for d in monitoring_devices if d.mgmt_ip}
    by_hostname = {d.hostname: d for d in monitoring_devices if d.hostname}
    by_name = {d.name: d for d in monitoring_devices if d.name}

    olts = []
    for olt in all_olts:
        status_label, status_variant = _find_linked_monitoring_status(
            olt=olt,
            by_mgmt_ip=by_mgmt_ip,
            by_hostname=by_hostname,
            by_name=by_name,
        )
        olts.append(
            {
            "id": str(olt.id),
            "name": olt.name,
            "hostname": olt.hostname,
            "vendor": olt.vendor,
            "model": olt.model,
            "mgmt_ip": olt.mgmt_ip,
            "is_active": bool(olt.is_active),
            "runtime_status_label": status_label,
            "runtime_status_variant": status_variant,
            "pon_ports": olt_stats.get(str(olt.id), {}).get("pon_ports", 0),
            "detail_url": f"/admin/network/olts/{olt.id}",
            "edit_url": f"/admin/network/olts/{olt.id}/edit",
        }
        )

    stats = {"total": len(olts), "active": sum(1 for o in olts if o["is_active"])}

    return {"olts": olts, "olt_stats": olt_stats, "stats": stats}
