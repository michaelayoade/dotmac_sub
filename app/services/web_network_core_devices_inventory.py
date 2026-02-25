"""Inventory and list/filter helpers for network devices pages."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.services import network as network_service

if TYPE_CHECKING:
    from app.models.network import Port

logger = logging.getLogger(__name__)


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
    olts = network_service.olt_devices.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    olt_stats = {}
    for olt in olts:
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

    stats = {"total": len(olts), "active": sum(1 for o in olts if o.is_active)}

    return {"olts": olts, "olt_stats": olt_stats, "stats": stats}

