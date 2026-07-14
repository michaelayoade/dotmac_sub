"""Inventory and list/filter helpers for network devices pages."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.models.network_monitoring import DeviceInterface, NetworkDevice
from app.services import network as network_service
from app.services.device_operational_status import (
    DEGRADED,
    DOWN,
    UP,
    annotate_operational_status,
    derive_olt_operational_status,
    derive_ont_operational_status,
    warmer_is_stale,
)
from app.services.network import cpe as cpe_service
from app.services.network.imported_service_ports import imported_service_port_summary
from app.services.status_presentation import device_operational_status_presentation

if TYPE_CHECKING:
    from app.models.network import Port

logger = logging.getLogger(__name__)


def _network_device_is_olt_candidate(device: NetworkDevice) -> bool:
    """Treat monitoring devices with names ending in OLT as OLT list members."""
    return device.name.strip().lower().endswith("olt")


def resolve_olt_device_for_network_device(
    db: Session, device: NetworkDevice
) -> OLTDevice:
    """Return a dedicated OLTDevice for a promoted NetworkDevice, creating one when missing."""
    if device.mgmt_ip:
        matched = db.scalars(
            select(OLTDevice).where(OLTDevice.mgmt_ip == device.mgmt_ip)
        ).first()
        if matched:
            return matched
    if device.hostname:
        matched = db.scalars(
            select(OLTDevice).where(OLTDevice.hostname == device.hostname)
        ).first()
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
        "notes": _promoted_olt_notes(device.notes),
        "is_active": False,
    }
    try:
        return network_service.olt_devices.create(db=db, payload=payload)
    except IntegrityError:
        db.rollback()
        if device.mgmt_ip:
            matched = db.scalars(
                select(OLTDevice).where(OLTDevice.mgmt_ip == device.mgmt_ip)
            ).first()
            if matched:
                return matched
        if device.hostname:
            matched = db.scalars(
                select(OLTDevice).where(OLTDevice.hostname == device.hostname)
            ).first()
            if matched:
                return matched
        matched = db.scalars(
            select(OLTDevice).where(OLTDevice.name == device.name)
        ).first()
        if matched:
            return matched
        raise


def _promoted_olt_notes(notes: str | None) -> str:
    marker = (
        "Auto-created from monitoring inventory. Complete the OLT config pack "
        "and activate before provisioning."
    )
    existing = str(notes or "").strip()
    if not existing:
        return marker
    if marker in existing:
        return existing
    return f"{existing}\n\n{marker}"


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


def _probe_state_presenter(*, enabled: bool, last_ok: bool | None) -> tuple[str, str]:
    """Map probe config/result to a user-facing label + tone."""
    if not enabled:
        return "Disabled", "disabled"
    if last_ok is True:
        return "OK", "ok"
    if last_ok is False:
        return "Fail", "fail"
    return "Unknown", "unknown"


def _monitoring_retired_probe_status() -> dict[str, str | None]:
    # The legacy per-device ICMP/SNMP probe source (Zabbix) was retired with
    # the native monitoring cutover; native reachability lives on the poll
    # columns / operational status instead.
    reason = "Legacy probe source is not configured"
    return {
        "ping_label": "Refresh pending",
        "ping_state": "unknown",
        "ping_reason": reason,
        "snmp_label": "Refresh pending",
        "snmp_state": "unknown",
        "snmp_reason": reason,
    }


def _build_legacy_probe_statuses(
    devices: list[dict[str, Any]],
) -> dict[str, dict[str, str | None]]:
    """Degraded ICMP/SNMP probe states for Network Devices core rows.

    The Zabbix probe source was retired with the native monitoring cutover, so
    every row degrades to a fixed refresh-pending placeholder until native
    reachability data is available.
    """
    return {
        device_id: _monitoring_retired_probe_status()
        for device in devices
        if (device_id := str(device.get("id") or "").strip())
    }


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
    seen_keys: set[tuple[str, str]] = set()

    monitoring_devices = list(
        db.scalars(select(NetworkDevice).order_by(NetworkDevice.name.asc())).all()
    )
    by_mgmt_ip = {d.mgmt_ip: d for d in monitoring_devices if d.mgmt_ip}
    by_hostname = {d.hostname: d for d in monitoring_devices if d.hostname}
    by_name = {d.name: d for d in monitoring_devices if d.name}
    warm_stale = warmer_is_stale()

    def _linked_monitoring(device: object) -> NetworkDevice | None:
        mgmt_ip = getattr(device, "mgmt_ip", None)
        hostname = getattr(device, "hostname", None)
        name = getattr(device, "name", None)
        return (
            (by_mgmt_ip.get(mgmt_ip) if mgmt_ip else None)
            or (by_hostname.get(hostname) if hostname else None)
            or (by_name.get(name) if name else None)
        )

    def _add_seen(kind: str, value: object | None) -> None:
        text = str(value or "").strip().lower()
        if text:
            seen_keys.add((kind, text))

    def _seen(kind: str, value: object | None) -> bool:
        text = str(value or "").strip().lower()
        return bool(text and (kind, text) in seen_keys)

    olts = network_service.olt_devices.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=500, offset=0
    )
    for olt in olts:
        linked = _linked_monitoring(olt)
        linked_live_status = getattr(linked, "live_status", None)
        linked_live_status = getattr(linked_live_status, "value", linked_live_status)
        operational = derive_olt_operational_status(
            olt,
            linked_live_status=linked_live_status,
            warm_stale=warm_stale,
        )
        devices.append(
            {
                "id": str(olt.id),
                "name": olt.name,
                "type": "olt",
                "serial_number": getattr(olt, "serial_number", None),
                "ip_address": getattr(olt, "mgmt_ip", None),
                "vendor": olt.vendor,
                "model": olt.model,
                "status": operational.status,
                "operational_reason": operational.reason,
                "status_presentation": operational.presentation,
                "last_seen": getattr(olt, "last_seen", None),
                "subscriber": None,
            }
        )
        _add_seen("id", olt.id)
        _add_seen("mgmt_ip", getattr(olt, "mgmt_ip", None))
        _add_seen("hostname", getattr(olt, "hostname", None))
        _add_seen("name", getattr(olt, "name", None))

    core_devices = [device for device in monitoring_devices if device.is_active]
    annotate_operational_status(core_devices)
    for device in core_devices:
        if _network_device_is_olt_candidate(device):
            continue
        if (
            _seen("mgmt_ip", getattr(device, "mgmt_ip", None))
            or _seen("hostname", getattr(device, "hostname", None))
            or _seen("name", getattr(device, "name", None))
        ):
            continue
        operational = cast(Any, device).operational
        devices.append(
            {
                "id": str(device.id),
                "name": device.name,
                "type": "core",
                "serial_number": device.serial_number,
                "ip_address": device.mgmt_ip,
                "vendor": device.vendor,
                "model": device.model,
                "status": operational.status,
                "operational_reason": operational.reason,
                "status_presentation": operational.presentation,
                "last_seen": device.last_ping_at or device.last_snmp_at,
                "subscriber": None,
            }
        )
        _add_seen("id", device.id)
        _add_seen("mgmt_ip", device.mgmt_ip)
        _add_seen("hostname", device.hostname)
        _add_seen("name", device.name)

    onts = network_service.ont_units.list(
        db=db,
        is_active=True,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    for ont in onts:
        operational = derive_ont_operational_status(ont)
        devices.append(
            {
                "id": str(ont.id),
                "name": getattr(ont, "name", None) or ont.serial_number,
                "type": "ont",
                "serial_number": ont.serial_number,
                "ip_address": getattr(ont, "ip_address", None),
                "vendor": ont.vendor,
                "model": ont.model,
                "status": operational.status,
                "operational_reason": operational.reason,
                "status_presentation": operational.presentation,
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
    inventory_subscriber_id = cpe_service.get_inventory_subscriber_id(db)
    for cpe in cpes:
        if (
            inventory_subscriber_id is not None
            and getattr(cpe, "subscriber_id", None) == inventory_subscriber_id
        ):
            continue
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
                "status": "unknown",
                "operational_reason": "operational_state_not_available",
                "status_presentation": device_operational_status_presentation(
                    "unknown"
                ),
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
        "core": sum(1 for d in devices if d["type"] == "core"),
        "olt": sum(1 for d in devices if d["type"] == "olt"),
        "ont": sum(1 for d in devices if d["type"] == "ont"),
        "cpe": sum(1 for d in devices if d["type"] == "cpe"),
        "up": sum(1 for d in devices if d["status"] == UP),
        "down": sum(1 for d in devices if d["status"] == DOWN),
        "degraded": sum(1 for d in devices if d["status"] == DEGRADED),
        "maintenance": sum(1 for d in devices if d["status"] == "maintenance"),
        "unknown": sum(1 for d in devices if d["status"] == "unknown"),
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
        "type": device_type,
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
    device_type: str | None = None,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
) -> list[dict]:
    """Return filtered devices for HTMX filter partial."""
    devices = collect_devices(db)
    return filter_devices(
        devices, device_type=device_type, search=search, status=status, vendor=vendor
    )


def olts_list_page_data(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict[str, object]:
    """Return OLT list payload with per-OLT stats."""
    per_page = min(max(int(per_page or 50), 10), 200)
    raw_olts = list(
        db.scalars(
            select(OLTDevice)
            .where(OLTDevice.is_active.is_(True))
            .order_by(OLTDevice.name.asc())
        ).all()
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
        for device in db.scalars(
            select(NetworkDevice).order_by(NetworkDevice.name)
        ).all()
        if device.is_active and _network_device_is_olt_candidate(device)
    ]
    promoted_olt_records = [
        resolve_olt_device_for_network_device(db, device) for device in promoted_olts
    ]
    all_olts_by_id = {str(olt.id): olt for olt in raw_olts}
    for olt in promoted_olt_records:
        all_olts_by_id[str(olt.id)] = olt

    all_olts = sorted(
        all_olts_by_id.values(), key=lambda olt: str(olt.name or "").lower()
    )
    monitoring_devices = list(db.scalars(select(NetworkDevice)).all())

    by_mgmt_ip = {d.mgmt_ip: d for d in monitoring_devices if d.mgmt_ip}
    by_hostname = {d.hostname: d for d in monitoring_devices if d.hostname}
    by_name = {d.name: d for d in monitoring_devices if d.name}

    linked_monitoring_by_olt_id: dict[str, NetworkDevice] = {}
    for olt in all_olts:
        linked = None
        if olt.mgmt_ip:
            linked = by_mgmt_ip.get(olt.mgmt_ip)
        if linked is None and olt.hostname:
            linked = by_hostname.get(olt.hostname)
        if linked is None and olt.name:
            linked = by_name.get(olt.name)
        if linked is not None:
            linked_monitoring_by_olt_id[str(olt.id)] = linked

    warm_stale = warmer_is_stale()

    linked_ids = [d.id for d in linked_monitoring_by_olt_id.values() if d.id]
    interfaces_by_device_id: dict[str, list[DeviceInterface]] = {}
    if linked_ids:
        interfaces = list(
            db.scalars(
                select(DeviceInterface).where(DeviceInterface.device_id.in_(linked_ids))
            ).all()
        )
        for iface in interfaces:
            interfaces_by_device_id.setdefault(str(iface.device_id), []).append(iface)

    # Apply detail-page style fallback: if no modeled PON ports exist for an OLT,
    # use discovered SNMP interfaces with PON-like naming.
    for olt in all_olts:
        olt_id = str(olt.id)
        db_count = int(olt_stats.get(olt_id, {}).get("pon_ports", 0))
        linked = linked_monitoring_by_olt_id.get(olt_id)
        snmp_count = 0
        if linked and linked.id:
            iface_rows = interfaces_by_device_id.get(str(linked.id), [])
            snmp_count = sum(
                1
                for iface in iface_rows
                if any(
                    token in f"{iface.name or ''} {iface.description or ''}".lower()
                    for token in ("pon", "gpon", "epon", "xgpon", "xgs")
                )
            )
        resolved_count = db_count if db_count > 0 else snmp_count
        olt_stats[olt_id] = {"pon_ports": resolved_count}

    olts = []
    for olt in all_olts:
        service_port_summary = imported_service_port_summary(db, olt_id=olt.id)
        linked = linked_monitoring_by_olt_id.get(str(olt.id))
        status_label, status_variant = _find_linked_monitoring_status(
            olt=olt,
            by_mgmt_ip=by_mgmt_ip,
            by_hostname=by_hostname,
            by_name=by_name,
        )
        ping_label, ping_state = _probe_state_presenter(
            enabled=bool(linked and linked.ping_enabled),
            last_ok=(linked.last_ping_ok if linked else None),
        )
        snmp_label, snmp_state = _probe_state_presenter(
            enabled=bool(linked and linked.snmp_enabled),
            last_ok=(linked.last_snmp_ok if linked else None),
        )
        linked_live_status = getattr(linked, "live_status", None)
        linked_live_status = getattr(linked_live_status, "value", linked_live_status)
        operational = derive_olt_operational_status(
            olt,
            linked_live_status=linked_live_status,
            warm_stale=warm_stale,
        )
        if operational.alarming:
            health_state = "attention"
            health_label = "Attention"
            health_reason = operational.reason.replace("_", " ").capitalize()
        elif operational.retry_pending:
            health_state = "retry_pending"
            health_label = "Refresh pending"
            health_reason = "Reachability evidence is stale or missing; refresh queued"
        else:
            health_state = "healthy"
            health_label = "Healthy"
            health_reason = "Current reachability evidence is positive"

        # The legacy runtime-health overlay (Zabbix telemetry) was retired
        # with the native monitoring cutover: rows keep their local monitoring
        # values, matching the empty overlay the unconfigured path produced.
        runtime_health: dict[str, object] = {}

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
                "runtime_health_label": health_label,
                "runtime_health_state": health_state,
                "runtime_health_reason": health_reason,
                "operational_status": operational.status,
                "runtime_operational_reason": operational.reason,
                "status_presentation": operational.presentation,
                "runtime_retry_pending": operational.retry_pending,
                "runtime_ping_label": ping_label,
                "runtime_ping_state": ping_state,
                "runtime_snmp_label": snmp_label,
                "runtime_snmp_state": snmp_state,
                "runtime_source": runtime_health.get("runtime_source", "Local"),
                "runtime_last_seen_at": runtime_health.get("runtime_last_seen_at"),
                "runtime_trigger_summary": runtime_health.get(
                    "runtime_trigger_summary"
                ),
                "runtime_ont_online": runtime_health.get("runtime_ont_online"),
                "runtime_ont_offline": runtime_health.get("runtime_ont_offline"),
                "runtime_ont_total": runtime_health.get("runtime_ont_total"),
                "runtime_ont_online_pct": runtime_health.get("runtime_ont_online_pct"),
                "runtime_low_signal": runtime_health.get("runtime_low_signal"),
                "runtime_pon_up": runtime_health.get("runtime_pon_up"),
                "runtime_pon_total": runtime_health.get("runtime_pon_total"),
                "pon_ports": olt_stats.get(str(olt.id), {}).get("pon_ports", 0),
                "imported_service_ports": service_port_summary["count"],
                "imported_service_ports_at": service_port_summary["last_imported_at"],
                "detail_url": f"/admin/network/olts/{olt.id}",
                "edit_url": f"/admin/network/olts/{olt.id}/edit",
            }
        )

    term = (search or "").strip().lower()
    filtered_olts = olts
    if term:
        filtered_olts = [
            item
            for item in filtered_olts
            if term in str(item.get("name") or "").lower()
            or term in str(item.get("hostname") or "").lower()
            or term in str(item.get("vendor") or "").lower()
            or term in str(item.get("model") or "").lower()
            or term in str(item.get("mgmt_ip") or "").lower()
        ]

    normalized_status = (status or "").strip().lower()
    if normalized_status == "attention":
        filtered_olts = [
            item
            for item in filtered_olts
            if item.get("runtime_health_state") == "attention"
        ]
    elif normalized_status in {"up", "online", "healthy"}:
        filtered_olts = [
            item for item in filtered_olts if item.get("operational_status") == UP
        ]
    elif normalized_status == "degraded":
        filtered_olts = [
            item for item in filtered_olts if item.get("operational_status") == DEGRADED
        ]
    elif normalized_status in {"down", "offline"}:
        filtered_olts = [
            item for item in filtered_olts if item.get("operational_status") == DOWN
        ]
    elif normalized_status == "retry_pending":
        filtered_olts = [
            item for item in filtered_olts if item.get("runtime_retry_pending")
        ]

    filtered_total = len(filtered_olts)
    total_pages = max(1, (filtered_total + per_page - 1) // per_page)
    current_page = min(max(page, 1), total_pages)
    page_start = (current_page - 1) * per_page
    paged_olts = filtered_olts[page_start : page_start + per_page]

    attention_items = [
        item for item in olts if item.get("runtime_health_state") == "attention"
    ]
    up_count = sum(1 for item in olts if item.get("operational_status") == UP)
    degraded_count = sum(
        1 for item in olts if item.get("operational_status") == DEGRADED
    )
    down_count = sum(1 for item in olts if item.get("operational_status") == DOWN)
    retry_pending_count = sum(1 for item in olts if item.get("runtime_retry_pending"))
    total_pon_ports = sum(int(item.get("pon_ports") or 0) for item in olts)  # type: ignore[call-overload]

    stats = {
        "total": filtered_total,
        "fleet_total": len(olts),
        "active": sum(1 for o in filtered_olts if o["is_active"]),
        "attention": len(attention_items),
        "up": up_count,
        "degraded": degraded_count,
        "down": down_count,
        "retry_pending": retry_pending_count,
        "total_pon_ports": total_pon_ports,
    }

    attention_summary = attention_items[:6]

    return {
        "olts": paged_olts,
        "olt_stats": olt_stats,
        "stats": stats,
        "attention_summary": attention_summary,
        "filters": {
            "search": search or "",
            "status": normalized_status,
        },
        "pagination": {
            "page": current_page,
            "per_page": per_page,
            "total": filtered_total,
            "total_pages": total_pages,
            "has_prev": current_page > 1,
            "has_next": current_page < total_pages,
        },
    }
