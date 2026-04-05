"""Admin web helpers for the PON interfaces snapshot page."""

from __future__ import annotations

import re
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntAssignment, PonPort
from app.models.network_monitoring import (
    DeviceInterface,
    InterfaceStatus,
    NetworkDevice,
)
from app.services.common import coerce_uuid

_PON_TOKENS = ("pon", "gpon", "epon", "xgpon", "xgs")
_ALIAS_PREFIX = "[[alias:"
_ALIAS_SUFFIX = "]]"


def _is_pon_like(text: str | None) -> bool:
    lowered = str(text or "").lower()
    return any(token in lowered for token in _PON_TOKENS)


def _extract_pon_hint(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(\d+/\d+/\d+)", str(value).strip())
    if match:
        return match.group(1)
    return None


def _normalize_pon_key(value: str | None) -> str:
    text = str(value or "").strip()
    hint = _extract_pon_hint(text)
    if hint:
        return hint.lower()
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _resolve_monitoring_devices(
    db: Session, olts: list[OLTDevice]
) -> dict[str, NetworkDevice]:
    devices = list(
        db.scalars(select(NetworkDevice).where(NetworkDevice.is_active.is_(True))).all()
    )
    by_ip = {
        str(device.mgmt_ip).strip(): device
        for device in devices
        if getattr(device, "mgmt_ip", None)
    }
    by_hostname = {
        str(device.hostname).strip().lower(): device
        for device in devices
        if getattr(device, "hostname", None)
    }
    by_name = {
        str(device.name).strip().lower(): device
        for device in devices
        if getattr(device, "name", None)
    }

    resolved: dict[str, NetworkDevice] = {}
    for olt in olts:
        device = None
        mgmt_ip = str(getattr(olt, "mgmt_ip", "") or "").strip()
        hostname = str(getattr(olt, "hostname", "") or "").strip().lower()
        name = str(getattr(olt, "name", "") or "").strip().lower()
        if mgmt_ip:
            device = by_ip.get(mgmt_ip)
        if device is None and hostname:
            device = by_hostname.get(hostname)
        if device is None and name:
            device = by_name.get(name)
        if device is not None:
            resolved[str(olt.id)] = device
    return resolved


def _load_monitoring_interfaces(
    db: Session,
    monitoring_devices: dict[str, NetworkDevice],
) -> dict[str, list[DeviceInterface]]:
    device_ids = [device.id for device in monitoring_devices.values()]
    if not device_ids:
        return {}
    interfaces = list(
        db.scalars(
            select(DeviceInterface)
            .where(DeviceInterface.device_id.in_(device_ids))
            .order_by(DeviceInterface.name.asc())
        ).all()
    )
    grouped: dict[str, list[DeviceInterface]] = defaultdict(list)
    for iface in interfaces:
        if _is_pon_like(f"{iface.name or ''} {iface.description or ''}"):
            grouped[str(iface.device_id)].append(iface)
    return grouped


def _status_label(status: str) -> str:
    return {
        "up": "Up",
        "down": "Down",
        "unknown": "Unknown",
    }.get(status, "Unknown")


def parse_pon_port_notes(notes: str | None) -> tuple[str | None, str | None]:
    """Extract alias metadata from notes while preserving user-facing notes text."""
    text = str(notes or "").strip()
    if not text:
        return None, None

    alias: str | None = None
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_ALIAS_PREFIX) and stripped.endswith(_ALIAS_SUFFIX):
            alias_value = stripped[len(_ALIAS_PREFIX) : -len(_ALIAS_SUFFIX)].strip()
            alias = alias_value or None
            continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip() or None
    return alias, cleaned


def merge_pon_port_notes(notes: str | None, alias: str | None) -> str | None:
    """Persist alias metadata in notes without losing the existing freeform notes body."""
    _existing_alias, cleaned_notes = parse_pon_port_notes(notes)
    parts: list[str] = []
    if cleaned_notes:
        parts.append(cleaned_notes)
    alias_text = (alias or "").strip()
    if alias_text:
        parts.append(f"{_ALIAS_PREFIX}{alias_text}{_ALIAS_SUFFIX}")
    merged = "\n".join(parts).strip()
    return merged or None


def _row_matches_search(row: dict[str, object], search: str) -> bool:
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("olt_name", "olt_ip", "name", "alias", "description", "status")
    ).lower()
    return search.lower() in haystack


def build_page_data(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    olt_id: str | None = None,
) -> dict[str, object]:
    filters = {
        "search": (search or "").strip(),
        "status": (status or "").strip(),
        "olt_id": (olt_id or "").strip(),
    }

    olt_stmt = (
        select(OLTDevice)
        .where(OLTDevice.is_active.is_(True))
        .order_by(OLTDevice.name.asc())
    )
    if filters["olt_id"]:
        olt_stmt = olt_stmt.where(OLTDevice.id == coerce_uuid(filters["olt_id"]))
    olts = list(db.scalars(olt_stmt).all())
    olts_by_id = {str(olt.id): olt for olt in olts}

    port_stmt = (
        select(PonPort)
        .where(PonPort.is_active.is_(True))
        .order_by(PonPort.olt_id.asc(), PonPort.name.asc())
    )
    if filters["olt_id"]:
        port_stmt = port_stmt.where(PonPort.olt_id == coerce_uuid(filters["olt_id"]))
    pon_ports = list(db.scalars(port_stmt).all())

    for port in pon_ports:
        if str(port.olt_id) not in olts_by_id:
            olt = db.get(OLTDevice, port.olt_id)
            if olt and olt.is_active:
                olts.append(olt)
                olts_by_id[str(olt.id)] = olt

    monitoring_devices = _resolve_monitoring_devices(db, olts)
    monitoring_interfaces = _load_monitoring_interfaces(db, monitoring_devices)

    assignment_counts: dict[str, int] = defaultdict(int)
    for assignment in db.scalars(
        select(OntAssignment).where(OntAssignment.active.is_(True))
    ).all():
        if getattr(assignment, "pon_port_id", None):
            assignment_counts[str(assignment.pon_port_id)] += 1

    rows: list[dict[str, object]] = []
    matched_monitoring_keys: set[tuple[str, str]] = set()

    interfaces_by_olt: dict[str, list[DeviceInterface]] = defaultdict(list)
    for olt_key, device in monitoring_devices.items():
        interfaces_by_olt[olt_key] = monitoring_interfaces.get(str(device.id), [])

    for port in pon_ports:
        olt = olts_by_id.get(str(port.olt_id))
        iface_match = None
        port_key = _normalize_pon_key(port.name)
        for iface in interfaces_by_olt.get(str(port.olt_id), []):
            iface_key = _normalize_pon_key(iface.name)
            if iface_key and iface_key == port_key:
                iface_match = iface
                matched_monitoring_keys.add((str(port.olt_id), iface_key))
                break

        status_value = (
            iface_match.status.value
            if iface_match and iface_match.status
            else InterfaceStatus.unknown.value
        )
        row = {
            "kind": "modeled",
            "pon_port_id": str(port.id),
            "olt_id": str(port.olt_id),
            "olt_name": getattr(olt, "name", "Unknown OLT"),
            "olt_ip": getattr(olt, "mgmt_ip", None),
            "name": port.name,
            "alias": parse_pon_port_notes(getattr(port, "notes", None))[0],
            "description": (iface_match.description if iface_match else None)
            or parse_pon_port_notes(getattr(port, "notes", None))[1],
            "status": status_value,
            "status_label": _status_label(status_value),
            "subscriber_count": assignment_counts.get(str(port.id), 0),
            "monitoring_seen": iface_match is not None,
            "onts_url": f"/admin/network/onts?olt_id={port.olt_id}&pon_port_id={port.id}",
        }
        rows.append(row)

    for olt_key, interfaces in interfaces_by_olt.items():
        olt = olts_by_id.get(olt_key)
        for iface in interfaces:
            iface_key = _normalize_pon_key(iface.name)
            if iface_key and (olt_key, iface_key) in matched_monitoring_keys:
                continue
            rows.append(
                {
                    "kind": "discovered",
                    "pon_port_id": None,
                    "olt_id": olt_key,
                    "olt_name": getattr(olt, "name", "Unknown OLT"),
                    "olt_ip": getattr(olt, "mgmt_ip", None),
                    "name": iface.name,
                    "alias": None,
                    "description": iface.description,
                    "status": iface.status.value
                    if iface.status
                    else InterfaceStatus.unknown.value,
                    "status_label": _status_label(
                        iface.status.value
                        if iface.status
                        else InterfaceStatus.unknown.value
                    ),
                    "subscriber_count": 0,
                    "monitoring_seen": True,
                    "onts_url": f"/admin/network/onts?olt_id={olt_key}&pon_hint={_extract_pon_hint(iface.name) or iface.name}",
                }
            )

    if filters["search"]:
        rows = [row for row in rows if _row_matches_search(row, filters["search"])]
    if filters["status"] in {"up", "down", "unknown"}:
        rows = [row for row in rows if row.get("status") == filters["status"]]
    if filters["olt_id"]:
        rows = [row for row in rows if row.get("olt_id") == filters["olt_id"]]

    rows.sort(
        key=lambda row: (
            str(row.get("olt_name") or "").lower(),
            0 if row.get("kind") == "modeled" else 1,
            0 if row.get("alias") else 1,
            str(row.get("alias") or "").lower(),
            str(row.get("name") or "").lower(),
        )
    )

    stats = {
        "total": len(rows),
        "up": sum(1 for row in rows if row.get("status") == "up"),
        "down": sum(1 for row in rows if row.get("status") == "down"),
        "unknown": sum(1 for row in rows if row.get("status") == "unknown"),
        "aliased": sum(1 for row in rows if row.get("alias")),
    }

    olt_options = [
        {"id": str(olt.id), "name": olt.name}
        for olt in sorted(olts_by_id.values(), key=lambda item: item.name.lower())
    ]

    return {
        "filters": filters,
        "stats": stats,
        "rows": rows,
        "olts": olt_options,
    }


def save_alias(
    db: Session,
    *,
    olt_id: str,
    interface_name: str,
    alias: str | None,
    pon_port_id: str | None = None,
) -> PonPort:
    alias_text = (alias or "").strip() or None
    interface_name = (interface_name or "").strip()
    if not interface_name:
        raise HTTPException(status_code=400, detail="Interface name is required")

    port: PonPort | None = None
    if pon_port_id:
        port = db.get(PonPort, coerce_uuid(pon_port_id))
        if port is not None and str(port.olt_id) != str(coerce_uuid(olt_id)):
            raise HTTPException(
                status_code=400,
                detail="PON port does not belong to the selected OLT",
            )
        if port is not None and not bool(getattr(port, "is_active", True)):
            raise HTTPException(
                status_code=400,
                detail="PON port is inactive",
            )
        if port is not None and str(getattr(port, "name", "") or "") != interface_name:
            raise HTTPException(
                status_code=400,
                detail="PON port does not match the submitted interface name",
            )
    if port is None:
        port = db.scalars(
            select(PonPort).where(
                PonPort.olt_id == coerce_uuid(olt_id),
                PonPort.name == interface_name,
                PonPort.is_active.is_(True),
            )
        ).first()
    if port is None:
        olt = db.get(OLTDevice, coerce_uuid(olt_id))
        if not olt:
            raise HTTPException(status_code=404, detail="OLT device not found")
        from app.services import web_network_olts as web_network_olts_service

        fsp_hint = _extract_pon_hint(interface_name)
        board = None
        port_number = None
        if fsp_hint:
            board, port_number = fsp_hint.rsplit("/", 1)
        port = web_network_olts_service.ensure_canonical_pon_port(
            db,
            olt_id=olt.id,
            fsp=fsp_hint or interface_name,
            board=board,
            port=port_number,
        )
        port.notes = merge_pon_port_notes(getattr(port, "notes", None), alias_text)
    else:
        port.notes = merge_pon_port_notes(getattr(port, "notes", None), alias_text)

    db.commit()
    db.refresh(port)
    return port
