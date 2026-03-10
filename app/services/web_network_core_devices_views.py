"""OLT/ONT/detail/consolidated helpers for core-network device web routes."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import CPEDevice
from app.models.network_monitoring import (
    DeviceInterface,
    NetworkDevice,
)
from app.services import network as network_service

logger = logging.getLogger(__name__)

_VM_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")


def _normalize_port_name(value: str | None) -> str:
    """Normalize interface/port names for loose matching."""
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _extract_range_display(*values: object) -> str | None:
    """Best-effort parser for optical range text like '20km' from free text."""
    for value in values:
        if not value:
            continue
        text = str(value)
        match = re.search(r"(\d+(?:\.\d+)?)\s*km\b", text, flags=re.IGNORECASE)
        if match:
            return f"{match.group(1)} km"
    return None


def _get_olt_health(olt_name: str) -> dict[str, Any]:
    """Fetch latest OLT health metrics from VictoriaMetrics.

    Returns a dict with cpu, temperature, memory, uptime values
    and formatted display strings. Gracefully returns empty on error.
    """
    result: dict[str, Any] = {
        "has_data": False,
        "cpu_percent": None,
        "temperature_c": None,
        "memory_percent": None,
        "uptime_seconds": None,
        "cpu_display": None,
        "temperature_display": None,
        "memory_display": None,
        "uptime_display": None,
        "temperature_status": "normal",
    }

    metrics = {
        "cpu_percent": f'olt_cpu_percent{{olt_name="{olt_name}"}}',
        "temperature_c": f'olt_temperature_celsius{{olt_name="{olt_name}"}}',
        "memory_percent": f'olt_memory_percent{{olt_name="{olt_name}"}}',
        "uptime_seconds": f'olt_uptime_seconds{{olt_name="{olt_name}"}}',
    }

    for key, query in metrics.items():
        try:
            resp = httpx.get(
                f"{_VM_URL}/api/v1/query",
                params={"query": query},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            if (
                isinstance(data, dict)
                and data.get("status") == "success"
            ):
                results = data.get("data", {}).get("result", [])
                if results and results[0].get("value"):
                    val = float(results[0]["value"][1])
                    result[key] = val
                    result["has_data"] = True
        except Exception:
            continue

    # Build display strings
    if result["cpu_percent"] is not None:
        result["cpu_display"] = f"{result['cpu_percent']:.0f}%"

    if result["temperature_c"] is not None:
        temp = result["temperature_c"]
        result["temperature_display"] = f"{temp:.0f}\u00b0C"
        if temp > 65:
            result["temperature_status"] = "critical"
        elif temp > 50:
            result["temperature_status"] = "warning"

    if result["memory_percent"] is not None:
        result["memory_display"] = f"{result['memory_percent']:.0f}%"

    if result["uptime_seconds"] is not None:
        secs = int(result["uptime_seconds"])
        days = secs // 86400
        hours = (secs % 86400) // 3600
        minutes = (secs % 3600) // 60
        if days > 0:
            result["uptime_display"] = f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            result["uptime_display"] = f"{hours}h {minutes}m"
        else:
            result["uptime_display"] = f"{minutes}m"

    return result


def olt_detail_page_data(db: Session, olt_id: str) -> dict[str, object] | None:
    """Return OLT detail payload with PON ports, ONT assignments, and signal data."""
    try:
        olt = network_service.olt_devices.get(db=db, device_id=olt_id)
    except HTTPException:
        return None

    pon_ports = network_service.pon_ports.list(
        db=db,
        olt_id=olt_id,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    # Gather ONT assignments and build per-port stats
    from app.services.network.olt_polling import classify_signal, get_signal_thresholds

    warn, crit = get_signal_thresholds(db)
    ont_assignments = []
    port_stats: dict[str, dict[str, int]] = {}

    for port in pon_ports:
        port_assignments = network_service.ont_assignments.list(
            db=db,
            pon_port_id=str(port.id),
            ont_unit_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        active_assignments = [a for a in port_assignments if a.active]
        ont_assignments.extend(active_assignments)

        # Per-port ONU summary
        p_online = 0
        p_offline = 0
        p_low_signal = 0
        p_signal_total = 0.0
        p_signal_count = 0
        for a in active_assignments:
            ont = a.ont_unit
            if not ont:
                continue
            status_val = getattr(ont, "online_status", None)
            s = status_val.value if status_val else "unknown"
            if s == "online":
                p_online += 1
            elif s == "offline":
                p_offline += 1
            quality = classify_signal(
                getattr(ont, "olt_rx_signal_dbm", None),
                warn_threshold=warn,
                crit_threshold=crit,
            )
            olt_rx_val = getattr(ont, "olt_rx_signal_dbm", None)
            if olt_rx_val is not None:
                try:
                    p_signal_total += float(olt_rx_val)
                    p_signal_count += 1
                except Exception:
                    pass
            if quality in ("warning", "critical"):
                p_low_signal += 1
        avg_signal = (p_signal_total / p_signal_count) if p_signal_count > 0 else None
        online_pct = int(round((p_online / len(active_assignments)) * 100)) if active_assignments else 0
        port_stats[str(port.id)] = {
            "total": len(active_assignments),
            "online": p_online,
            "offline": p_offline,
            "low_signal": p_low_signal,
            "avg_olt_rx_dbm": avg_signal,
            "online_pct": online_pct,
        }

    # Build signal data for each ONT assignment
    signal_data: dict[str, dict[str, object]] = {}
    total_online = 0
    total_offline = 0
    total_low_signal = 0
    for a in ont_assignments:
        ont = a.ont_unit
        if not ont:
            continue
        olt_rx = getattr(ont, "olt_rx_signal_dbm", None)
        onu_rx = getattr(ont, "onu_rx_signal_dbm", None)
        quality = classify_signal(olt_rx, warn_threshold=warn, crit_threshold=crit)
        status_val = getattr(ont, "online_status", None)
        s = status_val.value if status_val else "unknown"
        reason = getattr(ont, "offline_reason", None)
        reason_val = reason.value if reason else None
        signal_data[str(ont.id)] = {
            "olt_rx_dbm": olt_rx,
            "onu_rx_dbm": onu_rx,
            "quality": quality,
            "quality_class": SIGNAL_QUALITY_CLASSES.get(
                quality, SIGNAL_QUALITY_CLASSES["unknown"]
            ),
            "status": s,
            "status_class": ONLINE_STATUS_CLASSES.get(
                s, ONLINE_STATUS_CLASSES["unknown"]
            ),
            "reason_display": OFFLINE_REASON_DISPLAY.get(reason_val, "")
            if reason_val
            else "",
            "distance_meters": getattr(ont, "distance_meters", None),
            "signal_updated_at": getattr(ont, "signal_updated_at", None),
        }
        if s == "online":
            total_online += 1
        elif s == "offline":
            total_offline += 1
        if quality in ("warning", "critical"):
            total_low_signal += 1

    # Load shelf/card/port hierarchy via ORM relationships
    from app.models.network import OltShelf

    shelves = list(
        db.scalars(
            select(OltShelf)
            .where(OltShelf.olt_id == olt.id)
            .order_by(OltShelf.shelf_number)
        ).all()
    )

    ont_summary = {
        "total": len(ont_assignments),
        "online": total_online,
        "offline": total_offline,
        "low_signal": total_low_signal,
    }

    # Fetch OLT hardware health from VictoriaMetrics
    olt_health = _get_olt_health(olt.name)

    # Fetch recent config backups
    from app.models.network import OltConfigBackup

    # SNMP settings are stored on the linked core monitoring device record.
    monitoring_device = None
    if olt.mgmt_ip:
        monitoring_device = db.scalars(
            select(NetworkDevice).where(NetworkDevice.mgmt_ip == olt.mgmt_ip).limit(1)
        ).first()
    if monitoring_device is None and olt.hostname:
        monitoring_device = db.scalars(
            select(NetworkDevice).where(NetworkDevice.hostname == olt.hostname).limit(1)
        ).first()
    if monitoring_device is None and olt.name:
        monitoring_device = db.scalars(
            select(NetworkDevice).where(NetworkDevice.name == olt.name).limit(1)
        ).first()

    monitoring_data: dict[str, object] | None = None
    live_board_inventory: list[dict[str, object]] = []
    resolved_device_info = {
        "hostname": monitoring_device.hostname if monitoring_device and monitoring_device.hostname else olt.hostname,
        "mgmt_ip": monitoring_device.mgmt_ip if monitoring_device and monitoring_device.mgmt_ip else olt.mgmt_ip,
        "vendor": monitoring_device.vendor if monitoring_device and monitoring_device.vendor else olt.vendor,
        "model": monitoring_device.model if monitoring_device and monitoring_device.model else olt.model,
        "serial_number": monitoring_device.serial_number
        if monitoring_device and monitoring_device.serial_number
        else olt.serial_number,
        "status": monitoring_device.status.value if monitoring_device and monitoring_device.status else ("active" if olt.is_active else "inactive"),
        "last_ping_at": monitoring_device.last_ping_at if monitoring_device else None,
        "last_snmp_at": monitoring_device.last_snmp_at if monitoring_device else None,
        "last_ping_ok": monitoring_device.last_ping_ok if monitoring_device else None,
        "last_snmp_ok": monitoring_device.last_snmp_ok if monitoring_device else None,
    }

    if monitoring_device is not None:
        interfaces = list(
            db.scalars(
                select(DeviceInterface)
                .where(DeviceInterface.device_id == monitoring_device.id)
                .order_by(DeviceInterface.name.asc())
            ).all()
        )

        pon_interfaces: list[dict[str, object]] = []
        monitoring_interfaces: list[dict[str, object]] = []
        for iface in interfaces:
            item = {
                "id": str(iface.id),
                "name": iface.name,
                "description": iface.description,
                "status": iface.status.value if iface.status else "unknown",
                "speed_mbps": iface.speed_mbps,
                "mac_address": iface.mac_address,
                "updated_at": iface.updated_at,
            }
            monitoring_interfaces.append(item)
            text = f"{iface.name or ''} {iface.description or ''}".lower()
            if any(token in text for token in ("pon", "gpon", "epon", "xgpon", "xgs")):
                pon_interfaces.append(item)

        snmp_system: dict[str, object] | None = None
        if monitoring_device.snmp_enabled:
            try:
                from app.services.snmp_discovery import (
                    _parse_scalar,
                    _parse_walk,
                    _run_snmpbulkwalk,
                    _run_snmpwalk,
                )

                sys_name = _parse_scalar(_run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.1.5.0"))
                sys_descr = _parse_scalar(_run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.1.1.0"))
                sys_object_id = _parse_scalar(_run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.1.2.0"))
                sys_uptime = _parse_scalar(_run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.1.3.0"))
                sys_contact = _parse_scalar(_run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.1.4.0"))
                sys_location = _parse_scalar(_run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.1.6.0"))
                if_number = _parse_scalar(_run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.2.1.0"))

                snmp_system = {
                    "sys_name": sys_name,
                    "sys_descr": sys_descr,
                    "sys_object_id": sys_object_id,
                    "sys_uptime": sys_uptime,
                    "sys_contact": sys_contact,
                    "sys_location": sys_location,
                    "if_number": if_number,
                }

                ent_class = _parse_walk(
                    _run_snmpbulkwalk(monitoring_device, ".1.3.6.1.2.1.47.1.1.1.1.5")
                )
                ent_name = _parse_walk(
                    _run_snmpbulkwalk(monitoring_device, ".1.3.6.1.2.1.47.1.1.1.1.7")
                )
                ent_descr = _parse_walk(
                    _run_snmpbulkwalk(monitoring_device, ".1.3.6.1.2.1.47.1.1.1.1.2")
                )
                ent_model = _parse_walk(
                    _run_snmpbulkwalk(monitoring_device, ".1.3.6.1.2.1.47.1.1.1.1.13")
                )

                for idx, cls_raw in ent_class.items():
                    cls = re.search(r"(\d+)", cls_raw or "")
                    cls_code = int(cls.group(1)) if cls else None
                    name = (ent_name.get(idx) or "").strip()
                    descr = (ent_descr.get(idx) or "").strip()
                    model = (ent_model.get(idx) or "").strip()
                    merged = f"{name} {descr} {model}".strip()
                    if not merged:
                        continue
                    lowered = merged.lower()
                    looks_optics_like = any(
                        t in lowered
                        for t in (
                            "optic",
                            "sfp",
                            "qsfp",
                            "xfp",
                            "gpon_uni",
                            " epon_uni",
                            " tx ",
                            " rx ",
                            "nm",
                            "km",
                            " sc",
                            " lc",
                            "connector",
                        )
                    )
                    looks_chassis_like = any(
                        t in lowered
                        for t in (
                            "rack",
                            "subrack",
                            "chassis",
                            "frame",
                            "cabinet",
                            "shelf",
                            "enclosure",
                            "backplane",
                        )
                    )
                    slot_match = re.search(r"slot\s*([0-9]+)", lowered)
                    looks_like_card = any(
                        t in lowered
                        for t in (
                            "board",
                            " card",
                            "slot",
                            "service board",
                            "control board",
                            "main board",
                            "mpu",
                            "subrack",
                            "rack",
                        )
                    ) or (cls_code in {9} and not looks_optics_like and not looks_chassis_like)
                    if looks_chassis_like and not slot_match:
                        looks_like_card = False
                    if not looks_like_card:
                        continue
                    live_board_inventory.append(
                        {
                            "index": idx,
                            "slot_number": int(slot_match.group(1)) if slot_match else None,
                            "card_type": merged[:120],
                            "category": "card",
                        }
                    )
            except Exception:
                snmp_system = None

        monitoring_data = {
            "interfaces": monitoring_interfaces,
            "pon_interfaces": pon_interfaces,
            "snmp_system": snmp_system,
        }

    live_board_inventory.sort(
        key=lambda item: (
            0 if item.get("category") == "card" else 1,
            item.get("slot_number") is None,
            item.get("slot_number") if item.get("slot_number") is not None else 10**9,
            str(item.get("card_type") or "").lower(),
        )
    )
    live_board_cards = [item for item in live_board_inventory if item.get("category") == "card"]
    live_board_others: list[dict[str, object]] = []

    db_pon_count = len(pon_ports)
    snmp_pon_count = (
        len(monitoring_data.get("pon_interfaces", []))
        if isinstance(monitoring_data, dict)
        else 0
    )
    resolved_pon_ports_count = db_pon_count if db_pon_count > 0 else snmp_pon_count

    pon_port_table_rows: list[dict[str, object]] = []
    pon_snmp_by_norm_name: dict[str, dict[str, object]] = {}
    for iface in (monitoring_data.get("pon_interfaces", []) if isinstance(monitoring_data, dict) else []):
        norm = _normalize_port_name(str(iface.get("name") or ""))
        if norm and norm not in pon_snmp_by_norm_name:
            pon_snmp_by_norm_name[norm] = iface

    for port in pon_ports:
        ps = port_stats.get(str(port.id), {})
        iface = pon_snmp_by_norm_name.get(_normalize_port_name(getattr(port, "name", None)))
        card_port = getattr(port, "olt_card_port", None)
        sfp_modules = list(getattr(card_port, "sfp_modules", []) or []) if card_port else []
        active_sfps = [m for m in sfp_modules if getattr(m, "is_active", True)]
        tx_power_dbm = None
        for sfp in active_sfps:
            if getattr(sfp, "tx_power_dbm", None) is not None:
                tx_power_dbm = sfp.tx_power_dbm
                break
        if tx_power_dbm is None:
            for sfp in sfp_modules:
                if getattr(sfp, "tx_power_dbm", None) is not None:
                    tx_power_dbm = sfp.tx_power_dbm
                    break

        range_display = _extract_range_display(
            getattr(port, "notes", None),
            iface.get("description") if isinstance(iface, dict) else None,
            getattr(card_port, "name", None),
        )
        description = getattr(port, "notes", None) or (
            str(iface.get("description") or "").strip() if isinstance(iface, dict) else None
        )

        status_val = str((iface or {}).get("status") or "").lower() if isinstance(iface, dict) else ""
        if status_val not in {"up", "down"}:
            if int(ps.get("online", 0) or 0) > 0:
                status_val = "up"
            elif int(ps.get("total", 0) or 0) > 0:
                status_val = "down"
            else:
                status_val = "unknown"

        port_type = "PON"
        if card_port and getattr(card_port, "port_type", None):
            raw_type = getattr(card_port.port_type, "value", card_port.port_type)
            port_type = str(raw_type or "pon").replace("_", "-").upper()

        pon_port_table_rows.append(
            {
                "name": port.name,
                "type": port_type,
                "admin_state": "Enabled" if port.is_active else "Disabled",
                "status": status_val,
                "onus": int(ps.get("total", 0) or 0),
                "avg_signal_dbm": ps.get("avg_olt_rx_dbm"),
                "description": description,
                "range_display": range_display,
                "tx_power_dbm": tx_power_dbm,
                "action_url": f"/admin/network/onts?olt_id={olt.id}",
            }
        )

    if not pon_port_table_rows and isinstance(monitoring_data, dict):
        for iface in monitoring_data.get("pon_interfaces", []):
            description = str(iface.get("description") or "").strip() or None
            name_text = f"{iface.get('name') or ''} {description or ''}".lower()
            if "xgs" in name_text:
                port_type = "XGS-PON"
            elif "xgpon" in name_text or "xg-pon" in name_text:
                port_type = "XG-PON"
            elif "epon" in name_text:
                port_type = "EPON"
            else:
                port_type = "GPON/PON"
            pon_port_table_rows.append(
                {
                    "name": iface.get("name") or "N/A",
                    "type": port_type,
                    "admin_state": "N/A",
                    "status": str(iface.get("status") or "unknown").lower(),
                    "onus": 0,
                    "avg_signal_dbm": None,
                    "description": description,
                    "range_display": _extract_range_display(description),
                    "tx_power_dbm": None,
                    "action_url": f"/admin/network/onts?olt_id={olt.id}",
                }
            )

    config_backups = (
        db.query(OltConfigBackup)
        .filter(OltConfigBackup.olt_device_id == olt.id)
        .order_by(OltConfigBackup.created_at.desc())
        .limit(10)
        .all()
    )

    return {
        "olt": olt,
        "pon_ports": pon_ports,
        "ont_assignments": ont_assignments,
        "signal_data": signal_data,
        "port_stats": port_stats,
        "ont_summary": ont_summary,
        "shelves": shelves,
        "warn_threshold": warn,
        "crit_threshold": crit,
        "olt_health": olt_health,
        "monitoring_device": monitoring_device,
        "monitoring_data": monitoring_data,
        "resolved_device_info": resolved_device_info,
        "resolved_pon_ports_count": resolved_pon_ports_count,
        "live_board_inventory": live_board_inventory,
        "live_board_cards": live_board_cards,
        "live_board_others": live_board_others,
        "pon_port_table_rows": pon_port_table_rows,
        "config_backups": config_backups,
    }


def _classify_ont_signal(ont: object, warn: float, crit: float) -> str:
    """Classify ONT signal quality for template display."""
    from app.services.network.olt_polling import classify_signal

    dbm = getattr(ont, "olt_rx_signal_dbm", None)
    return classify_signal(dbm, warn_threshold=warn, crit_threshold=crit)


SIGNAL_QUALITY_CLASSES: dict[str, str] = {
    "good": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "warning": "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    "critical": "bg-rose-100 text-rose-800 dark:bg-rose-900 dark:text-rose-200",
    "unknown": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
}

ONLINE_STATUS_CLASSES: dict[str, str] = {
    "online": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "offline": "bg-rose-100 text-rose-800 dark:bg-rose-900 dark:text-rose-200",
    "unknown": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
}

OFFLINE_REASON_DISPLAY: dict[str, str] = {
    "power_fail": "Power Fail",
    "los": "Loss of Signal",
    "dying_gasp": "Dying Gasp",
    "unknown": "Unknown",
}


def onts_list_page_data(
    db: Session,
    *,
    status: str | None = None,
    olt_id: str | None = None,
    zone_id: str | None = None,
    online_status: str | None = None,
    signal_quality: str | None = None,
    search: str | None = None,
    vendor: str | None = None,
    order_by: str = "serial_number",
    order_dir: str = "asc",
    page: int = 1,
    per_page: int = 50,
) -> dict[str, object]:
    """Return ONT/CPE list payload with advanced filtering and signal classification."""
    from app.models.network import OLTDevice, OntUnit
    from app.services.network.olt_polling import get_signal_thresholds

    # Determine is_active from status filter
    status_filter = (status or "all").strip().lower()
    is_active: bool | None = None
    if status_filter == "active":
        is_active = True
    elif status_filter == "inactive":
        is_active = False

    # Calculate pagination offset
    offset = (max(page, 1) - 1) * per_page

    # Use advanced query with all filters
    onts: Sequence[OntUnit]
    onts, total_filtered = network_service.ont_units.list_advanced(
        db,
        olt_id=olt_id,
        zone_id=zone_id,
        signal_quality=signal_quality,
        online_status=online_status,
        vendor=vendor,
        search=search,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=per_page,
        offset=offset,
    )

    # Signal threshold classification for displayed ONTs
    warn, crit = get_signal_thresholds(db)
    signal_data: dict[str, dict[str, str]] = {}
    for ont in onts:
        quality = _classify_ont_signal(ont, warn, crit)
        ont_status_enum = getattr(ont, "online_status", None)
        status_val = ont_status_enum.value if ont_status_enum else "unknown"
        reason = getattr(ont, "offline_reason", None)
        reason_val = reason.value if reason else None
        signal_data[str(ont.id)] = {
            "quality": quality,
            "quality_class": SIGNAL_QUALITY_CLASSES.get(
                quality, SIGNAL_QUALITY_CLASSES["unknown"]
            ),
            "status_class": ONLINE_STATUS_CLASSES.get(
                status_val, ONLINE_STATUS_CLASSES["unknown"]
            ),
            "status_display": status_val.replace("_", " ").title()
            if status_val
            else "Unknown",
            "reason_display": OFFLINE_REASON_DISPLAY.get(reason_val, "")
            if reason_val
            else "",
        }

    # Summary counts (unfiltered) for KPI cards
    all_onts_count = db.scalar(select(func.count()).select_from(OntUnit)) or 0
    from app.models.network import OnuOnlineStatus

    online_count = (
        db.scalar(
            select(func.count())
            .select_from(OntUnit)
            .where(OntUnit.online_status == OnuOnlineStatus.online)
        )
        or 0
    )
    offline_count = (
        db.scalar(
            select(func.count())
            .select_from(OntUnit)
            .where(OntUnit.online_status == OnuOnlineStatus.offline)
        )
        or 0
    )
    low_signal_count = (
        db.scalar(
            select(func.count())
            .select_from(OntUnit)
            .where(OntUnit.olt_rx_signal_dbm < warn)
            .where(OntUnit.olt_rx_signal_dbm.isnot(None))
        )
        or 0
    )

    # CPEs (unchanged)
    cpes = network_service.cpe_devices.list(
        db=db,
        subscriber_id=None,
        subscription_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )

    stats = {
        "total_onts": all_onts_count,
        "total_cpes": len(cpes),
        "total": all_onts_count + len(cpes),
        "online_count": online_count,
        "offline_count": offline_count,
        "low_signal_count": low_signal_count,
    }

    # OLT list for filter dropdown
    olts = list(
        db.scalars(
            select(OLTDevice)
            .where(OLTDevice.is_active.is_(True))
            .order_by(OLTDevice.name)
        ).all()
    )

    # Zone list for filter dropdown
    from app.models.network import NetworkZone

    zones = list(
        db.scalars(
            select(NetworkZone)
            .where(NetworkZone.is_active.is_(True))
            .order_by(NetworkZone.name)
        ).all()
    )

    # Build active assignment lookup for OLT/PON display
    from app.models.network import OntAssignment, PonPort

    ont_ids = [ont.id for ont in onts]
    assignment_info: dict[str, dict[str, str]] = {}
    if ont_ids:
        assign_rows = db.execute(
            select(
                OntAssignment.ont_unit_id,
                OLTDevice.name.label("olt_name"),
                OLTDevice.id.label("olt_id"),
                PonPort.name.label("pon_port_name"),
            )
            .join(PonPort, PonPort.id == OntAssignment.pon_port_id)
            .join(OLTDevice, OLTDevice.id == PonPort.olt_id)
            .where(OntAssignment.active.is_(True))
            .where(OntAssignment.ont_unit_id.in_(ont_ids))
        ).all()
        for row in assign_rows:
            assignment_info[str(row.ont_unit_id)] = {
                "olt_name": row.olt_name,
                "olt_id": str(row.olt_id),
                "pon_port_name": row.pon_port_name,
            }

    # Pagination metadata
    total_pages = max(1, (total_filtered + per_page - 1) // per_page)

    # Distinct vendors for filter dropdown
    vendor_rows = db.scalars(
        select(OntUnit.vendor)
        .where(OntUnit.vendor.isnot(None))
        .where(OntUnit.vendor != "")
        .distinct()
        .order_by(OntUnit.vendor)
    ).all()

    return {
        "onts": onts,
        "cpes": cpes,
        "stats": stats,
        "status_filter": status_filter,
        "signal_data": signal_data,
        "assignment_info": assignment_info,
        "olts": olts,
        "vendors": list(vendor_rows),
        # Active filters for template state
        "zones": zones,
        # Active filters for template state
        "filters": {
            "olt_id": olt_id or "",
            "zone_id": zone_id or "",
            "online_status": online_status or "",
            "signal_quality": signal_quality or "",
            "search": search or "",
            "vendor": vendor or "",
            "order_by": order_by,
            "order_dir": order_dir,
        },
        # Pagination
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total_filtered,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
    }


def ont_detail_page_data(db: Session, ont_id: str) -> dict[str, object] | None:
    """Return comprehensive ONT detail payload.

    Includes: device info, active assignment, OLT/PON path, subscriber,
    subscription, signal classification, and network location.
    """
    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except Exception:
        return None

    assignments = network_service.ont_assignments.list(
        db=db,
        ont_unit_id=ont_id,
        pon_port_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assignment = next((a for a in assignments if a.active), None)
    past_assignments = [a for a in assignments if not a.active]

    # Signal classification
    from app.services.network.olt_polling import classify_signal, get_signal_thresholds

    warn, crit = get_signal_thresholds(db)
    olt_rx = getattr(ont, "olt_rx_signal_dbm", None)
    onu_rx = getattr(ont, "onu_rx_signal_dbm", None)
    olt_quality = classify_signal(olt_rx, warn_threshold=warn, crit_threshold=crit)
    onu_quality = classify_signal(onu_rx, warn_threshold=warn, crit_threshold=crit)
    ont_status = getattr(ont, "online_status", None)
    status_val = ont_status.value if ont_status else "unknown"
    reason = getattr(ont, "offline_reason", None)
    reason_val = reason.value if reason else None

    signal_info = {
        "olt_rx_dbm": olt_rx,
        "onu_rx_dbm": onu_rx,
        "olt_quality": olt_quality,
        "onu_quality": onu_quality,
        "olt_quality_class": SIGNAL_QUALITY_CLASSES.get(
            olt_quality, SIGNAL_QUALITY_CLASSES["unknown"]
        ),
        "onu_quality_class": SIGNAL_QUALITY_CLASSES.get(
            onu_quality, SIGNAL_QUALITY_CLASSES["unknown"]
        ),
        "distance_meters": getattr(ont, "distance_meters", None),
        "signal_updated_at": getattr(ont, "signal_updated_at", None),
        "online_status": status_val,
        "online_status_class": ONLINE_STATUS_CLASSES.get(
            status_val, ONLINE_STATUS_CLASSES["unknown"]
        ),
        "last_seen_at": getattr(ont, "last_seen_at", None),
        "offline_reason": reason_val,
        "offline_reason_display": OFFLINE_REASON_DISPLAY.get(reason_val, "")
        if reason_val
        else "",
        "warn_threshold": warn,
        "crit_threshold": crit,
    }

    # Build network path info (OLT → PON Port → Splitter → ONT)
    network_path: dict[str, object] = {}
    if assignment and assignment.pon_port:
        pon_port = assignment.pon_port
        network_path["pon_port"] = pon_port.name
        if pon_port.olt:
            network_path["olt_name"] = pon_port.olt.name
            network_path["olt_id"] = str(pon_port.olt.id)
            network_path["olt_vendor"] = pon_port.olt.vendor
        # Check for splitter link
        if hasattr(pon_port, "splitter_link") and pon_port.splitter_link:
            link = pon_port.splitter_link
            if hasattr(link, "splitter_port") and link.splitter_port:
                sp = link.splitter_port
                if hasattr(sp, "splitter") and sp.splitter:
                    network_path["splitter_name"] = sp.splitter.name or str(sp.splitter.id)[:8]

    # Subscriber and subscription info
    subscriber_info: dict[str, object] = {}
    if assignment and assignment.subscriber:
        sub = assignment.subscriber
        subscriber_info["id"] = str(sub.id)
        subscriber_info["name"] = _subscriber_display_name(sub)
        subscriber_info["status"] = sub.status.value if sub.status else "unknown"
        subscriber_info["status_class"] = ONLINE_STATUS_CLASSES.get(
            "online" if subscriber_info["status"] == "active" else "offline",
            ONLINE_STATUS_CLASSES["unknown"],
        )
    if assignment and assignment.subscription:
        subscription = assignment.subscription
        subscriber_info["subscription_id"] = str(subscription.id)
        subscriber_info["plan_name"] = (
            subscription.offer.name if hasattr(subscription, "offer") and subscription.offer else None
        )
        subscriber_info["subscription_status"] = (
            subscription.status.value if subscription.status else "unknown"
        )

    return {
        "ont": ont,
        "assignment": assignment,
        "past_assignments": past_assignments,
        "signal_info": signal_info,
        "network_path": network_path,
        "subscriber_info": subscriber_info,
    }


def _subscriber_display_name(subscriber: object) -> str:
    """Build display name from subscriber person or organization."""
    person = getattr(subscriber, "person", None)
    if person:
        first = getattr(person, "first_name", "") or ""
        last = getattr(person, "last_name", "") or ""
        name = f"{first} {last}".strip()
        if name:
            return name
    org = getattr(subscriber, "organization", None)
    if org:
        org_name = getattr(org, "name", None)
        if org_name:
            return str(org_name)
    return str(getattr(subscriber, "id", ""))[:8]


def get_change_request_asset(
    db: Session, asset_type: str | None, asset_id: str | None
) -> object | None:
    """Retrieve a fiber change request asset by type and id."""
    if not asset_type or not asset_id:
        return None
    from app.services import fiber_change_requests as change_requests

    _asset_type, model = change_requests._get_model(asset_type)
    return db.get(model, asset_id)


def consolidated_page_data(
    tab: str, db: Session, search: str | None = None
) -> dict[str, object]:
    """Return consolidated network-devices page payload."""
    term = (search or "").strip().lower()

    core_devices = db.scalars(
        select(NetworkDevice).order_by(NetworkDevice.name).limit(200)
    ).all()
    core_roles = {
        "core": len([d for d in core_devices if d.role and d.role.value == "core"]),
        "distribution": len(
            [d for d in core_devices if d.role and d.role.value == "distribution"]
        ),
        "access": len([d for d in core_devices if d.role and d.role.value == "access"]),
        "aggregation": len(
            [d for d in core_devices if d.role and d.role.value == "aggregation"]
        ),
        "edge": len([d for d in core_devices if d.role and d.role.value == "edge"]),
    }

    olts = network_service.olt_devices.list(
        db=db,
        is_active=None,
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

    active_onts = network_service.ont_units.list(
        db=db,
        is_active=True,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    inactive_onts = network_service.ont_units.list(
        db=db,
        is_active=False,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    onts = active_onts + inactive_onts
    cpes = db.scalars(
        select(CPEDevice).order_by(CPEDevice.created_at.desc()).limit(200)
    ).all()

    if term:

        def _contains(value: object | None) -> bool:
            return term in str(value or "").lower()

        core_devices = [
            device
            for device in core_devices
            if any(
                _contains(v)
                for v in [
                    device.name,
                    device.hostname,
                    device.mgmt_ip,
                    device.vendor,
                    device.model,
                    device.serial_number,
                    device.role.value if device.role else "",
                ]
            )
        ]

        olts = [
            olt
            for olt in olts
            if any(
                _contains(v)
                for v in [
                    olt.name,
                    olt.vendor,
                    olt.model,
                    olt.mgmt_ip,
                    getattr(olt, "management_ip", None),
                    getattr(olt, "location", None),
                ]
            )
        ]

        onts = [
            ont
            for ont in onts
            if any(
                _contains(v)
                for v in [
                    getattr(ont, "serial_number", None),
                    getattr(ont, "vendor", None),
                    getattr(ont, "model", None),
                    getattr(ont, "firmware_version", None),
                    getattr(ont, "notes", None),
                ]
            )
        ]

        cpes = [
            cpe
            for cpe in cpes
            if any(
                _contains(v)
                for v in [
                    getattr(cpe, "serial_number", None),
                    getattr(cpe, "vendor", None),
                    getattr(cpe, "model", None),
                    getattr(cpe, "mac_address", None),
                    getattr(cpe, "hostname", None),
                    getattr(cpe, "management_ip", None),
                    getattr(cpe, "wan_ip", None),
                    getattr(cpe, "ssid", None),
                    getattr(cpe, "notes", None),
                ]
            )
        ]

    stats = {
        "core_total": len(core_devices),
        "core_roles": core_roles,
        "olt_total": len(olts),
        "olt_active": sum(1 for o in olts if o.is_active),
        "ont_total": len(onts),
        "ont_inactive": len(inactive_onts),
        "cpe_total": len(cpes),
    }
    return {
        "tab": tab,
        "search": search or "",
        "stats": stats,
        "core_devices": core_devices,
        "olts": olts,
        "olt_stats": olt_stats,
        "onts": onts,
        "cpes": cpes,
    }


def _backup_notes_has_failure(notes: str | None) -> bool:
    if not notes:
        return False
    lowered = notes.lower()
    return any(token in lowered for token in ("fail", "error", "timeout", "denied"))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def backup_overview_page_data(
    db: Session,
    *,
    status: str | None = None,
    device_type: str | None = None,
    search: str | None = None,
    stale_hours: int = 24,
    sort: str = "last_backup_asc",
) -> dict[str, object]:
    """Return unified NAS/OLT backup overview rows for /admin/network/backups."""
    from app.models.catalog import NasConfigBackup, NasDevice
    from app.models.network import OltConfigBackup, OLTDevice

    cutoff = datetime.now(UTC) - timedelta(hours=max(stale_hours, 1))
    rows: list[dict[str, object]] = []
    term = (search or "").strip().lower()
    status_filter = (status or "all").strip().lower()
    device_type_filter = (device_type or "all").strip().lower()

    nas_devices = list(db.scalars(select(NasDevice).order_by(NasDevice.name.asc())).all())
    for device in nas_devices:
        latest = db.execute(
            select(NasConfigBackup)
            .where(NasConfigBackup.nas_device_id == device.id)
            .order_by(NasConfigBackup.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        last_backup_at = _as_utc(latest.created_at) if latest else None
        last_message = (
            f"{latest.backup_method.value.upper() if latest.backup_method else 'MANUAL'} "
            f"backup ({latest.config_size_bytes or 0} bytes)"
            if latest
            else "No backup snapshot found"
        )
        failed = bool(
            latest
            and (
                (latest.config_size_bytes is not None and latest.config_size_bytes <= 0)
                or _backup_notes_has_failure(latest.notes)
            )
        )
        is_stale = (last_backup_at is None) or (last_backup_at < cutoff)
        backup_status = "failed" if failed else ("stale" if is_stale else "success")
        rows.append(
            {
                "id": f"nas:{device.id}",
                "device_id": str(device.id),
                "backup_id": str(latest.id) if latest else None,
                "device_name": device.name,
                "device_type": "nas",
                "group": device.pop_site.name if device.pop_site else "-",
                "vendor": device.vendor.value if getattr(device, "vendor", None) else None,
                "model": device.model,
                "ip_address": device.management_ip or device.ip_address or "-",
                "port": device.management_port or "-",
                "last_backup_at": last_backup_at,
                "last_message": last_message,
                "backup_status": backup_status,
                "device_url": f"/admin/network/nas/devices/{device.id}",
                "backup_url": f"/admin/network/nas/backups/{latest.id}" if latest else None,
                "history_url": f"/admin/network/nas/devices/{device.id}/backups",
            }
        )

    olts = list(db.scalars(select(OLTDevice).order_by(OLTDevice.name.asc())).all())
    for olt in olts:
        latest = db.execute(
            select(OltConfigBackup)
            .where(OltConfigBackup.olt_device_id == olt.id)
            .order_by(OltConfigBackup.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        last_backup_at = _as_utc(latest.created_at) if latest else None
        last_message = (
            f"{latest.backup_type.value.title()} backup ({latest.file_size_bytes or 0} bytes)"
            if latest
            else "No backup snapshot found"
        )
        failed = bool(
            latest
            and (
                (latest.file_size_bytes is not None and latest.file_size_bytes <= 0)
                or _backup_notes_has_failure(latest.notes)
            )
        )
        is_stale = (last_backup_at is None) or (last_backup_at < cutoff)
        backup_status = "failed" if failed else ("stale" if is_stale else "success")
        rows.append(
            {
                "id": f"olt:{olt.id}",
                "device_id": str(olt.id),
                "backup_id": str(latest.id) if latest else None,
                "device_name": olt.name,
                "device_type": "olt",
                "group": "-",
                "vendor": olt.vendor,
                "model": olt.model,
                "ip_address": olt.mgmt_ip or "-",
                "port": "-",
                "last_backup_at": last_backup_at,
                "last_message": last_message,
                "backup_status": backup_status,
                "device_url": f"/admin/network/olts/{olt.id}",
                "backup_url": None,
                "history_url": f"/admin/network/olts/{olt.id}",
            }
        )

    if device_type_filter in {"nas", "olt"}:
        rows = [row for row in rows if row["device_type"] == device_type_filter]
    if status_filter in {"success", "stale", "failed"}:
        rows = [row for row in rows if row["backup_status"] == status_filter]
    if term:
        rows = [
            row
            for row in rows
            if term in " ".join(
                str(value or "").lower()
                for value in (
                    row["device_name"],
                    row["device_type"],
                    row["group"],
                    row["vendor"],
                    row["model"],
                    row["ip_address"],
                    row["last_message"],
                )
            )
        ]

    min_ts = datetime.min.replace(tzinfo=UTC)
    if sort == "last_backup_desc":
        rows.sort(key=lambda row: row["last_backup_at"] or min_ts, reverse=True)
    else:
        rows.sort(key=lambda row: row["last_backup_at"] or min_ts)

    stats = {
        "total": len(rows),
        "success": sum(1 for row in rows if row["backup_status"] == "success"),
        "stale": sum(1 for row in rows if row["backup_status"] == "stale"),
        "failed": sum(1 for row in rows if row["backup_status"] == "failed"),
        "nas": sum(1 for row in rows if row["device_type"] == "nas"),
        "olt": sum(1 for row in rows if row["device_type"] == "olt"),
    }
    return {
        "rows": rows,
        "stats": stats,
        "status_filter": status_filter if status_filter in {"success", "stale", "failed"} else "all",
        "device_type_filter": device_type_filter if device_type_filter in {"nas", "olt"} else "all",
        "search_filter": search or "",
        "stale_hours": max(stale_hours, 1),
        "sort_filter": sort if sort in {"last_backup_asc", "last_backup_desc"} else "last_backup_asc",
    }
