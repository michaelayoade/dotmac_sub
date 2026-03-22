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
from sqlalchemy.orm import Session, joinedload

from app.models.network import CPEDevice
from app.models.network_monitoring import (
    DeviceInterface,
    NetworkDevice,
)
from app.models.provisioning import ProvisioningRun
from app.services import network as network_service
from app.services.web_network_core_devices_inventory import (
    _network_device_is_olt_candidate,
    resolve_olt_device_for_network_device,
)

logger = logging.getLogger(__name__)

_VM_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")


def _summarize_provisioning_run(run: ProvisioningRun) -> dict[str, object]:
    payload = run.output_payload if isinstance(run.output_payload, dict) else {}
    raw_results = payload.get("results") if isinstance(payload.get("results"), list) else []
    step_results: list[dict[str, str]] = []
    success_count = 0
    failed_count = 0
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status") or "").strip().lower()
        if status == "success":
            success_count += 1
        elif status == "failed":
            failed_count += 1
        step_results.append(
            {
                "step_type": str(raw.get("step_type") or raw.get("name") or "step"),
                "status": status or "unknown",
                "detail": str(raw.get("detail") or raw.get("message") or "").strip(),
            }
        )
    return {
        "id": str(run.id),
        "workflow_name": run.workflow.name if run.workflow else "Provisioning Workflow",
        "status": run.status.value if run.status else "unknown",
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "error_message": run.error_message or "",
        "step_results": step_results[:6],
        "step_count": len(step_results),
        "success_count": success_count,
        "failed_count": failed_count,
    }


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


def _extract_tx_power_dbm(*values: object) -> float | None:
    """Best-effort parser for TX power text like 'Tx -2.3 dBm'."""
    for value in values:
        if not value:
            continue
        text = str(value)
        tx_match = re.search(r"tx[^-0-9]*(-?\d+(?:\.\d+)?)\s*d?bm", text, flags=re.IGNORECASE)
        if tx_match:
            try:
                return float(tx_match.group(1))
            except ValueError:
                continue
    return None


def _decode_huawei_packed_fsp(packed_value: int) -> str | None:
    """Best-effort decode of Huawei packed FSP index to frame/slot/port."""
    if packed_value < 0:
        return None
    base = 0xFA000000
    if packed_value < base:
        return None
    delta = packed_value - base
    if delta % 256 != 0:
        return None
    slot_port = delta // 256
    frame = 0
    slot = slot_port // 16
    port = slot_port % 16
    if slot < 0 or port < 0:
        return None
    return f"{frame}/{slot}/{port}"


def _normalize_ont_port_display(board: object, port: object) -> str | None:
    """Build display-safe PON port text from ONT board/port values."""
    board_text = str(board or "").strip()
    port_text = str(port or "").strip()
    if not board_text or not port_text:
        return None
    if board_text == "0/0" and port_text.isdigit():
        decoded = _decode_huawei_packed_fsp(int(port_text))
        if decoded:
            return decoded
    return f"{board_text}/{port_text}"


def _pon_port_display_text(pon_port: object | None) -> str | None:
    """Return PON port label as '<last-port-segment> - <description>' when possible."""
    if not pon_port:
        return None
    from app.services.web_network_pon_interfaces import parse_pon_port_notes

    alias_text, cleaned_notes = parse_pon_port_notes(getattr(pon_port, "notes", None))
    notes_text = str(cleaned_notes or "").strip()
    name_text = str(getattr(pon_port, "name", "") or "").strip()
    if alias_text:
        if name_text:
            port_segment = name_text.split("/")[-1].strip() or name_text
            return f"{port_segment} - {alias_text}"
        return alias_text
    if notes_text:
        if name_text:
            port_segment = name_text.split("/")[-1].strip() or name_text
            return f"{port_segment} - {notes_text}"
        return notes_text
    return name_text or None


def _extract_port_index(value: object | None) -> int | None:
    """Extract final numeric PON port index from mixed strings."""
    text = str(value or "").strip()
    if not text:
        return None
    hint = _extract_pon_hint(text)
    if hint:
        token = hint.split("/")[-1]
        return int(token) if token.isdigit() else None
    if "/" in text:
        token = text.split("/")[-1].strip()
        if token.isdigit():
            return int(token)
    trailing_num = re.search(r"(\d+)\s*$", text)
    if trailing_num:
        return int(trailing_num.group(1))
    return None


def _pon_port_table_label(
    name: object | None,
    *,
    port_number: object | None = None,
    fallback_index: int | None = None,
) -> str:
    """Return compact port label for PON table (prefer numeric port index)."""
    if port_number is not None:
        return str(port_number)
    parsed = _extract_port_index(name)
    if parsed is not None:
        return str(parsed)
    if fallback_index is not None:
        return str(fallback_index)
    name_text = str(name or "").strip()
    if not name_text:
        return "N/A"
    return name_text


def _extract_pon_hint(value: str | None) -> str | None:
    """Extract canonical F/S/P-like hint from interface names."""
    if not value:
        return None
    match = re.search(r"(\d+/\d+/\d+)\s*$", str(value).strip())
    if match:
        return match.group(1)
    return None


def _parse_composite_index(raw_index: str) -> tuple[str, ...]:
    """Parse dotted SNMP table index into numeric components."""
    parts = [p for p in str(raw_index).strip().split(".") if p.isdigit()]
    return tuple(parts)


def _snmp_index_to_fsp(raw_index: str, packed_fsp_map: dict[str, str] | None = None) -> str | None:
    """Best-effort map SNMP composite index to frame/slot/port string."""
    parts = _parse_composite_index(raw_index)
    if len(parts) >= 3:
        return f"{parts[0]}/{parts[1]}/{parts[2]}"
    if len(parts) == 2 and packed_fsp_map:
        return packed_fsp_map.get(parts[0])
    return None


def _parse_snmp_signal_dbm(raw_value: str | None, *, scale: float = 0.01) -> float | None:
    """Parse SNMP integer optical power into dBm."""
    if not raw_value:
        return None
    match = re.search(r"(-?\d+)", str(raw_value))
    if not match:
        return None
    try:
        raw_int = int(match.group(1))
    except ValueError:
        return None
    dbm = raw_int * scale
    if -50.0 <= dbm <= 10.0:
        return dbm
    if -50.0 <= raw_int <= 10.0:
        return float(raw_int)
    return None


def _parse_walk_composite(lines: Sequence[str], *, suffix_parts: int = 4) -> dict[str, str]:
    """Parse SNMP walk lines while preserving composite table indexes."""
    parsed: dict[str, str] = {}
    for line in lines:
        if " = " not in line:
            continue
        oid_part, value_part = line.split(" = ", 1)
        oid_tokens = [p for p in oid_part.split(".") if p.isdigit()]
        if not oid_tokens:
            continue
        if len(oid_tokens) >= 2 and int(oid_tokens[-2]) > 1_000_000:
            # Huawei packed index format: <packed_fsp>.<onu_id>
            index = f"{oid_tokens[-2]}.{oid_tokens[-1]}"
        else:
            index = ".".join(oid_tokens[-suffix_parts:]) if len(oid_tokens) >= suffix_parts else oid_tokens[-1]
        value = value_part.split(": ", 1)[-1].strip().strip('"')
        if value.lower().startswith("no such"):
            continue
        parsed[index] = value
    return parsed


def _pon_sort_key(hint: str) -> tuple[int, int, int]:
    parts = hint.split("/")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except Exception:
        return (10**9, 10**9, 10**9)


def _build_packed_fsp_map(indexes: Sequence[str], pon_hints: Sequence[str]) -> dict[str, str]:
    packed_values: list[int] = []
    for idx in indexes:
        parts = [p for p in str(idx).split(".") if p.isdigit()]
        if len(parts) == 2:
            try:
                packed_values.append(int(parts[0]))
            except ValueError:
                continue
    unique_packed = sorted(set(packed_values))
    sorted_hints = sorted({h for h in pon_hints if h}, key=_pon_sort_key)
    return {str(p): h for p, h in zip(unique_packed, sorted_hints, strict=False)}


def _huawei_snmp_pon_live_stats(
    monitoring_device: object | None,
    pon_hints: Sequence[str] | None = None,
) -> tuple[dict[str, int], dict[str, float]]:
    """Return per-PON ONU count and average OLT RX dBm using Huawei SNMP OIDs."""
    if monitoring_device is None or not getattr(monitoring_device, "snmp_enabled", False):
        return {}, {}
    vendor = str(getattr(monitoring_device, "vendor", "") or "").lower()
    if "huawei" not in vendor:
        return {}, {}

    try:
        from app.services.snmp_discovery import _run_snmpwalk

        # Huawei GPON: ONU run status and OLT RX power per ONU.
        status_rows = _parse_walk_composite(
            _run_snmpwalk(monitoring_device, ".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.15")
        )
        olt_rx_rows = _parse_walk_composite(
            _run_snmpwalk(monitoring_device, ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4")
        )
    except Exception:
        return {}, {}

    onu_count_by_fsp: dict[str, int] = {}
    signal_sums: dict[str, float] = {}
    signal_counts: dict[str, int] = {}
    packed_fsp_map = _build_packed_fsp_map(
        list(status_rows.keys()) + list(olt_rx_rows.keys()),
        list(pon_hints or []),
    )

    for raw_index in status_rows:
        fsp = _snmp_index_to_fsp(raw_index, packed_fsp_map)
        if not fsp:
            continue
        onu_count_by_fsp[fsp] = onu_count_by_fsp.get(fsp, 0) + 1

    for raw_index, raw_value in olt_rx_rows.items():
        fsp = _snmp_index_to_fsp(raw_index, packed_fsp_map)
        if not fsp:
            continue
        dbm = _parse_snmp_signal_dbm(raw_value, scale=0.01)
        if dbm is None:
            continue
        signal_sums[fsp] = signal_sums.get(fsp, 0.0) + dbm
        signal_counts[fsp] = signal_counts.get(fsp, 0) + 1

    avg_signal_by_fsp = {
        fsp: (signal_sums[fsp] / signal_counts[fsp])
        for fsp in signal_counts
        if signal_counts[fsp] > 0
    }
    return onu_count_by_fsp, avg_signal_by_fsp


def _ont_pon_hints(ont: object) -> set[str]:
    """Build possible F/S/P hints from ONT fields."""
    hints: set[str] = set()
    board_raw = str(getattr(ont, "board", "") or "").strip()
    port_raw = str(getattr(ont, "port", "") or "").strip()
    channel_raw = str(getattr(ont, "gpon_channel", "") or "").strip()

    if board_raw and port_raw:
        hints.add(f"{board_raw}/{port_raw}")
        if "/" in board_raw:
            hints.add(f"{board_raw}/{port_raw}")
        if board_raw.isdigit():
            # Common MA5800 layout assumption: frame/channel first (often 0)
            frame = channel_raw if channel_raw.isdigit() else "0"
            hints.add(f"{frame}/{board_raw}/{port_raw}")
    if port_raw and re.search(r"\d+/\d+/\d+", port_raw):
        hints.add(port_raw)
    if board_raw and re.search(r"\d+/\d+/\d+", board_raw):
        hints.add(board_raw)

    return {h for h in hints if h}


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
    assignment_by_ont_id: dict[str, object] = {}
    port_stats: dict[str, dict[str, int]] = {}

    for port_idx, port in enumerate(pon_ports):
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
        for a in active_assignments:
            if getattr(a, "ont_unit_id", None):
                assignment_by_ont_id[str(a.ont_unit_id)] = a

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
                    logger.debug(
                        "Could not parse OLT RX signal value for ONT %s: %r",
                        getattr(ont, "id", None),
                        olt_rx_val,
                        exc_info=True,
                    )
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

    # Include ONTs directly linked to OLT even when not assigned to a PON port.
    from app.models.network import OntUnit

    direct_onts = list(
        db.scalars(
            select(OntUnit)
            .where(OntUnit.olt_device_id == olt.id)
            .where(OntUnit.is_active.is_(True))
            .order_by(OntUnit.serial_number.asc())
        ).all()
    )
    onts_by_id = {
        str(ont.id): ont
        for ont in direct_onts
        if getattr(ont, "id", None)
    }
    for a in ont_assignments:
        ont = getattr(a, "ont_unit", None)
        if ont and getattr(ont, "id", None):
            onts_by_id[str(ont.id)] = ont
    onts_on_olt = list(onts_by_id.values())

    # Build signal data for each ONT
    signal_data: dict[str, dict[str, object]] = {}
    pon_port_display_by_ont_id: dict[str, str] = {}
    ont_mac_by_ont_id: dict[str, str] = {}
    assignment_subscription_ids = {
        getattr(a, "subscription_id", None)
        for a in ont_assignments
        if getattr(a, "subscription_id", None) is not None
    }
    assignment_subscriber_ids = {
        getattr(a, "subscriber_id", None)
        for a in ont_assignments
        if getattr(a, "subscriber_id", None) is not None
    }
    cpe_mac_by_subscription_id: dict[object, str] = {}
    cpe_mac_by_subscriber_id: dict[object, str] = {}
    if assignment_subscription_ids:
        cpes_by_subscription = list(
            db.scalars(
                select(CPEDevice)
                .where(CPEDevice.subscription_id.in_(assignment_subscription_ids))
                .order_by(CPEDevice.updated_at.desc(), CPEDevice.created_at.desc())
            ).all()
        )
        for cpe in cpes_by_subscription:
            subscription_id = getattr(cpe, "subscription_id", None)
            mac = str(getattr(cpe, "mac_address", "") or "").strip()
            if subscription_id is None or not mac:
                continue
            if subscription_id not in cpe_mac_by_subscription_id:
                cpe_mac_by_subscription_id[subscription_id] = mac
    if assignment_subscriber_ids:
        cpes_by_subscriber = list(
            db.scalars(
                select(CPEDevice)
                .where(CPEDevice.subscriber_id.in_(assignment_subscriber_ids))
                .order_by(CPEDevice.updated_at.desc(), CPEDevice.created_at.desc())
            ).all()
        )
        for cpe in cpes_by_subscriber:
            subscriber_id = getattr(cpe, "subscriber_id", None)
            mac = str(getattr(cpe, "mac_address", "") or "").strip()
            if subscriber_id is None or not mac:
                continue
            if subscriber_id not in cpe_mac_by_subscriber_id:
                cpe_mac_by_subscriber_id[subscriber_id] = mac
    total_online = 0
    total_offline = 0
    total_low_signal = 0
    for ont in onts_on_olt:
        ont_id = str(ont.id)
        ont_mac = str(getattr(ont, "mac_address", "") or "").strip()
        if ont_mac:
            ont_mac_by_ont_id[ont_id] = ont_mac
        assignment = assignment_by_ont_id.get(ont_id)
        if assignment:
            subscription_id = getattr(assignment, "subscription_id", None)
            subscriber_id = getattr(assignment, "subscriber_id", None)
            cpe_mac = None
            if subscription_id is not None:
                cpe_mac = cpe_mac_by_subscription_id.get(subscription_id)
            if not cpe_mac and subscriber_id is not None:
                cpe_mac = cpe_mac_by_subscriber_id.get(subscriber_id)
            if cpe_mac:
                ont_mac_by_ont_id[ont_id] = cpe_mac
        if assignment and getattr(assignment, "pon_port", None):
            port_display = _pon_port_display_text(assignment.pon_port)
            if port_display:
                pon_port_display_by_ont_id[ont_id] = port_display
        else:
            normalized_port = _normalize_ont_port_display(
                getattr(ont, "board", None),
                getattr(ont, "port", None),
            )
            if normalized_port:
                pon_port_display_by_ont_id[ont_id] = normalized_port

        olt_rx = getattr(ont, "olt_rx_signal_dbm", None)
        onu_rx = getattr(ont, "onu_rx_signal_dbm", None)
        quality = classify_signal(olt_rx, warn_threshold=warn, crit_threshold=crit)
        status_val = getattr(ont, "online_status", None)
        s = status_val.value if status_val else "unknown"
        reason = getattr(ont, "offline_reason", None)
        reason_val = reason.value if reason else None
        distance_meters = getattr(ont, "distance_meters", None)
        # Normalize stale/offline sentinel distance values to unknown for display.
        if s == "offline" and distance_meters is not None and distance_meters <= 1:
            distance_meters = None
        signal_data[ont_id] = {
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
            "distance_meters": distance_meters,
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
        "total": len(onts_on_olt),
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
        "firmware_version": olt.firmware_version,
        "software_version": olt.software_version,
        "supported_pon_types": getattr(olt, "supported_pon_types", None),
        "status": monitoring_device.status.value if monitoring_device and monitoring_device.status else ("active" if olt.is_active else "inactive"),
        "last_ping_at": monitoring_device.last_ping_at if monitoring_device else None,
        "last_snmp_at": monitoring_device.last_snmp_at if monitoring_device else None,
        "last_ping_ok": monitoring_device.last_ping_ok if monitoring_device else None,
        "last_snmp_ok": monitoring_device.last_snmp_ok if monitoring_device else None,
        "ping_enabled": bool(monitoring_device.ping_enabled) if monitoring_device else False,
        "snmp_enabled": bool(monitoring_device.snmp_enabled) if monitoring_device else False,
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

    ont_count_by_port_index: dict[int, int] = {}
    ont_signal_sum_by_port_index: dict[int, float] = {}
    ont_signal_count_by_port_index: dict[int, int] = {}
    for ont in onts_on_olt:
        ont_port_index = _extract_port_index(
            _normalize_ont_port_display(
                getattr(ont, "board", None),
                getattr(ont, "port", None),
            )
        )
        if ont_port_index is None:
            continue
        ont_count_by_port_index[ont_port_index] = ont_count_by_port_index.get(ont_port_index, 0) + 1
        ont_signal = getattr(ont, "olt_rx_signal_dbm", None)
        if ont_signal is None:
            continue
        try:
            ont_signal_float = float(ont_signal)
        except (TypeError, ValueError):
            continue
        ont_signal_sum_by_port_index[ont_port_index] = (
            ont_signal_sum_by_port_index.get(ont_port_index, 0.0) + ont_signal_float
        )
        ont_signal_count_by_port_index[ont_port_index] = (
            ont_signal_count_by_port_index.get(ont_port_index, 0) + 1
        )

    for port_idx, port in enumerate(pon_ports):
        ps = port_stats.get(str(port.id), {})
        iface = pon_snmp_by_norm_name.get(_normalize_port_name(getattr(port, "name", None)))
        row_port_index = _extract_port_index(getattr(port, "port_number", None))
        if row_port_index is None:
            row_port_index = _extract_port_index(getattr(port, "name", None))
        assigned_total = int(ps.get("total", 0) or 0)
        fallback_total = ont_count_by_port_index.get(row_port_index, 0) if row_port_index is not None else 0
        resolved_total = assigned_total if assigned_total > 0 else fallback_total
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
        if tx_power_dbm is None:
            tx_power_dbm = _extract_tx_power_dbm(
                iface.get("description") if isinstance(iface, dict) else None,
                getattr(port, "notes", None),
                getattr(card_port, "name", None),
            )

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

        avg_signal_dbm = ps.get("avg_olt_rx_dbm")
        if avg_signal_dbm is None and row_port_index is not None:
            signal_count = ont_signal_count_by_port_index.get(row_port_index, 0)
            if signal_count > 0:
                avg_signal_dbm = ont_signal_sum_by_port_index[row_port_index] / signal_count

        pon_port_table_rows.append(
            {
                "name": _pon_port_table_label(
                    getattr(port, "name", None),
                    port_number=getattr(port, "port_number", None),
                    fallback_index=port_idx,
                ),
                "type": port_type,
                "admin_state": "Enabled" if port.is_active else "Disabled",
                "status": status_val,
                "onus": resolved_total,
                "avg_signal_dbm": avg_signal_dbm,
                "description": description,
                "range_display": range_display,
                "tx_power_dbm": tx_power_dbm,
                "action_url": f"/admin/network/onts?olt_id={olt.id}&pon_port_id={port.id}",
            }
        )

    if not pon_port_table_rows and isinstance(monitoring_data, dict):
        snmp_onu_count_by_fsp, snmp_avg_signal_by_fsp = _huawei_snmp_pon_live_stats(
            monitoring_device,
            [
                h
                for iface in monitoring_data.get("pon_interfaces", [])
                if (h := _extract_pon_hint(str(iface.get("name") or "")))
            ],
        )

        ont_candidates = list(onts_on_olt)

        for iface_idx, iface in enumerate(monitoring_data.get("pon_interfaces", [])):
            description = str(iface.get("description") or "").strip() or None
            name_text = f"{iface.get('name') or ''} {description or ''}".lower()
            pon_hint = _extract_pon_hint(str(iface.get("name") or ""))

            matched_onts: list[Any] = []
            if pon_hint:
                matched_onts = [
                    ont
                    for ont in ont_candidates
                    if pon_hint in _ont_pon_hints(ont)
                ]

            signal_values = [
                float(ont.olt_rx_signal_dbm)
                for ont in matched_onts
                if getattr(ont, "olt_rx_signal_dbm", None) is not None
            ]
            avg_signal_dbm = (
                sum(signal_values) / len(signal_values) if signal_values else None
            )
            if avg_signal_dbm is None and pon_hint:
                avg_signal_dbm = snmp_avg_signal_by_fsp.get(pon_hint)

            distance_values = [
                int(ont.distance_meters)
                for ont in matched_onts
                if getattr(ont, "distance_meters", None) is not None
            ]
            range_display = _extract_range_display(description)
            if not range_display and distance_values:
                range_display = f"{(max(distance_values) / 1000):.1f} km"

            tx_power_dbm = _extract_tx_power_dbm(
                description,
                str(iface.get("name") or ""),
            )

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
                    "name": _pon_port_table_label(
                        iface.get("name"),
                        fallback_index=iface_idx,
                    ),
                    "type": port_type,
                    "admin_state": "N/A",
                    "status": str(iface.get("status") or "unknown").lower(),
                    "onus": len(matched_onts)
                    if matched_onts
                    else int(snmp_onu_count_by_fsp.get(str(pon_hint or ""), 0)),
                    "avg_signal_dbm": avg_signal_dbm,
                    "description": description,
                    "range_display": range_display,
                    "tx_power_dbm": tx_power_dbm,
                    "action_url": (
                        f"/admin/network/onts?olt_id={olt.id}&pon_hint="
                        f"{(pon_hint or str(iface.get('name') or ''))}"
                    ),
                }
            )

    # ONT display: when port descriptions exist, render as "<index> - <description>".
    desc_by_index: dict[int, str] = {}
    for idx, row in enumerate(pon_port_table_rows):
        desc = str((row or {}).get("description") or "").strip()
        if desc:
            desc_by_index[idx] = desc
    if desc_by_index:
        for ont in onts_on_olt:
            ont_id = str(getattr(ont, "id", ""))
            if not ont_id:
                continue
            port_index = _extract_port_index(
                _normalize_ont_port_display(
                    getattr(ont, "board", None),
                    getattr(ont, "port", None),
                )
            )
            if port_index is None:
                continue
            desc = desc_by_index.get(port_index)
            if desc:
                pon_port_display_by_ont_id[ont_id] = f"{port_index} - {desc}"

    config_backups = (
        db.query(OltConfigBackup)
        .filter(OltConfigBackup.olt_device_id == olt.id)
        .order_by(OltConfigBackup.created_at.desc())
        .limit(10)
        .all()
    )

    # VLANs and IP pools scoped to this OLT
    from app.models.network import IpPool, Vlan

    olt_vlans = list(
        db.scalars(
            select(Vlan)
            .where(Vlan.olt_device_id == olt.id)
            .order_by(Vlan.tag.asc())
        ).all()
    )
    olt_ip_pools = list(
        db.scalars(
            select(IpPool)
            .where(IpPool.olt_device_id == olt.id)
            .order_by(IpPool.name.asc())
        ).all()
    )
    available_vlans = list(
        db.scalars(
            select(Vlan)
            .where(Vlan.olt_device_id.is_(None))
            .where(Vlan.is_active.is_(True))
            .order_by(Vlan.tag.asc())
        ).all()
    )
    available_ip_pools = list(
        db.scalars(
            select(IpPool)
            .where(IpPool.olt_device_id.is_(None))
            .where(IpPool.is_active.is_(True))
            .order_by(IpPool.name.asc())
        ).all()
    )

    return {
        "olt": olt,
        "pon_ports": pon_ports,
        "ont_assignments": ont_assignments,
        "assignment_by_ont_id": assignment_by_ont_id,
        "pon_port_display_by_ont_id": pon_port_display_by_ont_id,
        "ont_mac_by_ont_id": ont_mac_by_ont_id,
        "onts_on_olt": onts_on_olt,
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
        "olt_vlans": olt_vlans,
        "olt_ip_pools": olt_ip_pools,
        "available_vlans": available_vlans,
        "available_ip_pools": available_ip_pools,
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

SUBSCRIBER_STATUS_CLASSES: dict[str, str] = {
    "active": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "new": "bg-sky-100 text-sky-800 dark:bg-sky-900 dark:text-sky-200",
    "suspended": "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    "delinquent": "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    "disabled": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
    "canceled": "bg-rose-100 text-rose-800 dark:bg-rose-900 dark:text-rose-200",
    "unknown": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
}

OFFLINE_REASON_DISPLAY: dict[str, str] = {
    "power_fail": "Power Fail",
    "los": "Loss of Signal",
    "dying_gasp": "Dying Gasp",
    "unknown": "Unknown",
}


def _is_synthetic_ont_serial(value: object | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(re.match(r"^(HW|ZT|NK|OLT)-[A-F0-9]{8}-\d+$", text))


def onts_list_page_data(
    db: Session,
    *,
    view: str = "list",
    status: str | None = None,
    olt_id: str | None = None,
    pon_port_id: str | None = None,
    pon_hint: str | None = None,
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

    normalized_view = "diagnostics" if view == "diagnostics" else "list"

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
        pon_port_id=pon_port_id,
        pon_hint=pon_hint,
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

    diagnostics_onts: Sequence[OntUnit] = ()
    diagnostics_total = 0
    if normalized_view == "diagnostics":
        diagnostics_onts, diagnostics_total = network_service.ont_units.list_advanced(
            db,
            olt_id=olt_id,
            pon_port_id=pon_port_id,
            pon_hint=pon_hint,
            zone_id=zone_id,
            signal_quality=signal_quality,
            online_status=online_status,
            vendor=vendor,
            search=search,
            is_active=is_active,
            order_by="signal",
            order_dir="asc",
            limit=per_page,
            offset=offset,
        )

    # Signal threshold classification for displayed ONTs
    warn, crit = get_signal_thresholds(db)
    signal_data: dict[str, dict[str, str]] = {}
    for ont in list(onts) + [item for item in diagnostics_onts if item not in onts]:
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

    # Summary counts aligned to current non-status filters.
    base_filter_kwargs = {
        "olt_id": olt_id,
        "pon_port_id": pon_port_id,
        "pon_hint": pon_hint,
        "zone_id": zone_id,
        "vendor": vendor,
        "search": search,
        "is_active": is_active,
        "order_by": order_by,
        "order_dir": order_dir,
        "limit": 1,
        "offset": 0,
    }
    _rows_ignored, all_onts_count = network_service.ont_units.list_advanced(
        db,
        online_status=None,
        signal_quality=None,
        **base_filter_kwargs,
    )
    _rows_ignored, online_count = network_service.ont_units.list_advanced(
        db,
        online_status="online",
        signal_quality=None,
        **base_filter_kwargs,
    )
    _rows_ignored, offline_count = network_service.ont_units.list_advanced(
        db,
        online_status="offline",
        signal_quality=None,
        **base_filter_kwargs,
    )
    _rows_ignored, warning_count = network_service.ont_units.list_advanced(
        db,
        online_status=None,
        signal_quality="warning",
        **base_filter_kwargs,
    )
    _rows_ignored, critical_count = network_service.ont_units.list_advanced(
        db,
        online_status=None,
        signal_quality="critical",
        **base_filter_kwargs,
    )
    low_signal_count = int(warning_count) + int(critical_count)

    total_cpes_count = db.scalar(select(func.count()).select_from(CPEDevice)) or 0

    stats = {
        "total_onts": all_onts_count,
        "total_cpes": total_cpes_count,
        "total": all_onts_count + total_cpes_count,
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

    ont_ids = list({ont.id for ont in list(onts) + list(diagnostics_onts)})
    assignment_info: dict[str, dict[str, str]] = {}
    serial_display_by_ont_id: dict[str, str] = {}
    for ont in list(onts) + [item for item in diagnostics_onts if item not in onts]:
        serial_value = str(getattr(ont, "serial_number", "") or "").strip()
        if serial_value:
            serial_display_by_ont_id[str(ont.id)] = serial_value
            continue
        mac_value = str(getattr(ont, "mac_address", "") or "").strip()
        if mac_value:
            serial_display_by_ont_id[str(ont.id)] = mac_value
        else:
            serial_display_by_ont_id[str(ont.id)] = "-"
    if ont_ids:
        assign_rows = db.scalars(
            select(OntAssignment)
            .options(
                joinedload(OntAssignment.subscriber),
                joinedload(OntAssignment.pon_port).joinedload(PonPort.olt),
            )
            .where(OntAssignment.active.is_(True))
            .where(OntAssignment.ont_unit_id.in_(ont_ids))
        ).all()
        for assignment in assign_rows:
            pon_port = assignment.pon_port
            olt = pon_port.olt if pon_port else None
            pon_number = (
                str(pon_port.port_number)
                if pon_port and pon_port.port_number is not None
                else _pon_port_table_label(getattr(pon_port, "name", None))
            )
            pon_description = str(getattr(pon_port, "notes", None) or "").strip()
            pon_display = (
                f"{pon_number} - {pon_description}"
                if pon_description
                else str(pon_number or getattr(pon_port, "name", None) or "-")
            )
            subscriber = assignment.subscriber
            assignment_info[str(assignment.ont_unit_id)] = {
                "olt_name": getattr(olt, "name", None),
                "olt_id": str(olt.id) if olt else "",
                "pon_port_name": getattr(pon_port, "name", None),
                "pon_port_display": pon_display,
                "subscriber_name": _subscriber_display_name(subscriber) if subscriber else "",
                "subscriber_customer_url": (
                    f"/admin/customers/organization/{subscriber.organization_id}"
                    if subscriber and getattr(subscriber, "organization_id", None)
                    else f"/admin/customers/person/{subscriber.id}"
                    if subscriber
                    else ""
                ),
            }

    # Pagination metadata
    total_pages = max(1, (total_filtered + per_page - 1) // per_page)
    diagnostics_total_pages = max(1, (diagnostics_total + per_page - 1) // per_page)

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
        "diagnostics_onts": diagnostics_onts,
        "stats": stats,
        "status_filter": status_filter,
        "signal_data": signal_data,
        "assignment_info": assignment_info,
        "serial_display_by_ont_id": serial_display_by_ont_id,
        "olts": olts,
        "vendors": list(vendor_rows),
        # Active filters for template state
        "zones": zones,
        # Active filters for template state
        "filters": {
            "olt_id": olt_id or "",
            "pon_port_id": pon_port_id or "",
            "pon_hint": pon_hint or "",
            "zone_id": zone_id or "",
            "online_status": online_status or "",
            "signal_quality": signal_quality or "",
            "search": search or "",
            "vendor": vendor or "",
            "view": normalized_view,
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
        "diagnostics_pagination": {
            "page": page,
            "per_page": per_page,
            "total": diagnostics_total,
            "total_pages": diagnostics_total_pages,
            "has_prev": page > 1,
            "has_next": page < diagnostics_total_pages,
        },
    }


def ont_detail_page_data(db: Session, ont_id: str) -> dict[str, object] | None:
    """Return comprehensive ONT detail payload.

    Includes: device info, active assignment, OLT/PON path, subscriber,
    subscription, signal classification, and network location.
    """
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
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
        subscriber_info["customer_url"] = (
            f"/admin/customers/organization/{sub.organization_id}"
            if getattr(sub, "organization_id", None)
            else f"/admin/customers/person/{sub.id}"
        )
        subscriber_info["name"] = _subscriber_display_name(sub)
        subscriber_info["status"] = sub.status.value if sub.status else "unknown"
        subscriber_info["status_class"] = SUBSCRIBER_STATUS_CLASSES.get(
            str(subscriber_info["status"]),
            SUBSCRIBER_STATUS_CLASSES["unknown"],
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

    # Provisioning profile info
    provisioning_info: dict[str, object] = {}
    if ont.provisioning_profile_id:
        provisioning_info["profile_id"] = str(ont.provisioning_profile_id)
        provisioning_info["status"] = (
            ont.provisioning_status.value if ont.provisioning_status else None
        )
        provisioning_info["last_provisioned_at"] = ont.last_provisioned_at
        if ont.provisioning_profile:
            provisioning_info["profile_name"] = ont.provisioning_profile.name

        # Check for drift
        from app.services.network.ont_profile_apply import detect_drift

        drift = detect_drift(db, str(ont.id))
        if drift:
            provisioning_info["has_drift"] = drift.has_drift
            provisioning_info["drifted_fields"] = [
                {"field": f.field_name, "desired": str(f.desired), "observed": str(f.observed)}
                for f in drift.drifted_fields
            ]

    # Available profiles for "Apply Profile" dropdown
    from app.services.network.ont_provisioning_profiles import ont_provisioning_profiles

    available_profiles = ont_provisioning_profiles.list(db, is_active=True, limit=50)

    provisioning_runs: list[dict[str, object]] = []
    subscription_id = getattr(assignment, "subscription_id", None) if assignment else None
    if subscription_id:
        raw_runs = (
            db.query(ProvisioningRun)
            .filter(ProvisioningRun.subscription_id == subscription_id)
            .order_by(ProvisioningRun.started_at.desc(), ProvisioningRun.created_at.desc())
            .limit(5)
            .all()
        )
        provisioning_runs = [_summarize_provisioning_run(run) for run in raw_runs]

    # Available firmware images matching this ONT's vendor
    from app.models.network import OntFirmwareImage

    ont_vendor = str(getattr(ont, "vendor", "") or "").strip()
    firmware_stmt = (
        select(OntFirmwareImage)
        .where(OntFirmwareImage.is_active.is_(True))
        .order_by(OntFirmwareImage.vendor, OntFirmwareImage.version.desc())
    )
    if ont_vendor:
        firmware_stmt = firmware_stmt.where(
            OntFirmwareImage.vendor.ilike(f"%{ont_vendor}%")
        )
    available_firmware = list(db.scalars(firmware_stmt.limit(20)).all())

    # Vendor capabilities for feature badges
    from app.services.network.ont_read import OntReadFacade

    capabilities = OntReadFacade.get_capabilities(db, ont_id)

    return {
        "ont": ont,
        "assignment": assignment,
        "past_assignments": past_assignments,
        "signal_info": signal_info,
        "network_path": network_path,
        "subscriber_info": subscriber_info,
        "provisioning_info": provisioning_info,
        "provisioning_runs": provisioning_runs,
        "available_profiles": available_profiles,
        "available_firmware": available_firmware,
        "capabilities": capabilities,
    }


def _subscriber_display_name(subscriber: object) -> str:
    """Build display name from subscriber person or organization."""
    display_name = str(getattr(subscriber, "display_name", "") or "").strip()
    if display_name:
        return display_name

    full_name = str(getattr(subscriber, "full_name", "") or "").strip()
    if full_name:
        return full_name

    first = str(getattr(subscriber, "first_name", "") or "").strip()
    last = str(getattr(subscriber, "last_name", "") or "").strip()
    direct_name = f"{first} {last}".strip()
    if direct_name:
        return direct_name

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
    email = str(getattr(subscriber, "email", "") or "").strip()
    if email:
        return email
    subscriber_number = str(getattr(subscriber, "subscriber_number", "") or "").strip()
    if subscriber_number:
        return subscriber_number
    return str(getattr(subscriber, "id", "") or "").strip()[:8]


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

    all_monitoring_devices = list(db.scalars(select(NetworkDevice).order_by(NetworkDevice.name)).all())
    promoted_olts = [
        resolve_olt_device_for_network_device(db, device)
        for device in all_monitoring_devices
        if device.is_active and _network_device_is_olt_candidate(device)
    ]
    promoted_olt_keys = {
        (
            str(getattr(device, "mgmt_ip", "") or "").strip(),
            str(getattr(device, "hostname", "") or "").strip(),
            str(getattr(device, "name", "") or "").strip(),
        )
        for device in all_monitoring_devices
        if device.is_active and _network_device_is_olt_candidate(device)
    }
    core_devices = [
        device
        for device in all_monitoring_devices
        if (
            str(getattr(device, "mgmt_ip", "") or "").strip(),
            str(getattr(device, "hostname", "") or "").strip(),
            str(getattr(device, "name", "") or "").strip(),
        )
        not in promoted_olt_keys
    ][:200]
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

    raw_olts = network_service.olt_devices.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    olts_by_id = {str(olt.id): olt for olt in raw_olts}
    for olt in promoted_olts:
        olts_by_id[str(olt.id)] = olt
    olts = sorted(olts_by_id.values(), key=lambda olt: str(getattr(olt, "name", "") or "").lower())
    monitoring_devices = all_monitoring_devices
    by_mgmt_ip = {d.mgmt_ip: d for d in monitoring_devices if d.mgmt_ip}
    by_hostname = {d.hostname: d for d in monitoring_devices if d.hostname}
    by_name = {d.name: d for d in monitoring_devices if d.name}
    monitoring_device_ids = [d.id for d in monitoring_devices]
    interfaces_by_device_id: dict[str, list[DeviceInterface]] = {}
    if monitoring_device_ids:
        iface_rows = list(
            db.scalars(
                select(DeviceInterface).where(DeviceInterface.device_id.in_(monitoring_device_ids))
            ).all()
        )
        for iface in iface_rows:
            key = str(iface.device_id)
            interfaces_by_device_id.setdefault(key, []).append(iface)

    def _linked_monitoring(olt_obj: object) -> NetworkDevice | None:
        mgmt_ip = getattr(olt_obj, "mgmt_ip", None)
        hostname = getattr(olt_obj, "hostname", None)
        name = getattr(olt_obj, "name", None)
        if mgmt_ip and mgmt_ip in by_mgmt_ip:
            return by_mgmt_ip[mgmt_ip]
        if hostname and hostname in by_hostname:
            return by_hostname[hostname]
        if name and name in by_name:
            return by_name[name]
        return None

    def _snmp_pon_count(olt_obj: object) -> int:
        linked = _linked_monitoring(olt_obj)
        if linked is None:
            return 0
        interfaces = interfaces_by_device_id.get(str(linked.id), [])
        return sum(
            1
            for iface in interfaces
            if any(
                token in f"{iface.name or ''} {iface.description or ''}".lower()
                for token in ("pon", "gpon", "epon", "xgpon", "xgs")
            )
        )

    olt_stats = {}
    for olt in olts:
        linked_monitor = _linked_monitoring(olt)
        if linked_monitor and linked_monitor.status:
            olt.runtime_status = linked_monitor.status.value
        else:
            # Keep unknown when we have no linked monitoring telemetry.
            olt.runtime_status = "unknown"

        pon_ports = network_service.pon_ports.list(
            db=db,
            olt_id=str(olt.id),
            is_active=True,
            order_by="created_at",
            order_dir="asc",
            limit=1000,
            offset=0,
        )
        db_count = len(pon_ports)
        resolved_count = db_count if db_count > 0 else _snmp_pon_count(olt)
        olt_stats[str(olt.id)] = {"pon_ports": resolved_count}

    ont_limit = 5000 if term else 500
    active_onts = network_service.ont_units.list(
        db=db,
        is_active=True,
        order_by="serial_number",
        order_dir="asc",
        limit=ont_limit,
        offset=0,
    )
    inactive_onts = network_service.ont_units.list(
        db=db,
        is_active=False,
        order_by="serial_number",
        order_dir="asc",
        limit=ont_limit,
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
