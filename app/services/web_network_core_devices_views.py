"""OLT/ONT/detail/consolidated helpers for core-network device web routes."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import (
    NasDevice,
    NasDeviceStatus,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import CPEDevice
from app.models.network_monitoring import (
    DeviceInterface,
    DeviceRole,
    DeviceStatus,
    NetworkDevice,
)
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.subscriber import SubscriberCategory
from app.models.tr069 import Tr069CpeDevice
from app.services import network as network_service
from app.services.network._common import decode_huawei_hex_serial, encode_to_hex_serial
from app.services.network.ont_bundle_assignments import get_active_bundle_assignment
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.olt_polling_parsers import _decode_huawei_packed_fsp
from app.services.network.ont_status import (
    resolve_effective_last_seen_at,
    resolve_ont_status_for_model,
)
from app.services.web_network_core_devices_inventory import (
    _network_device_is_olt_candidate,
    resolve_olt_device_for_network_device,
)

logger = logging.getLogger(__name__)

_VM_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")


def _nas_status_to_monitoring_status(status: NasDeviceStatus | None) -> DeviceStatus:
    if status == NasDeviceStatus.maintenance:
        return DeviceStatus.maintenance
    if status == NasDeviceStatus.active:
        return DeviceStatus.online
    return DeviceStatus.offline


def _nas_inventory_stub(device: NasDevice) -> SimpleNamespace:
    return SimpleNamespace(
        id=device.id,
        name=device.name,
        hostname=device.code,
        mgmt_ip=device.management_ip or device.ip_address or device.nas_ip,
        vendor=device.vendor.value if device.vendor else None,
        model=device.model,
        serial_number=device.serial_number,
        role=DeviceRole.access,
        status=_nas_status_to_monitoring_status(device.status),
        detail_url=f"/admin/network/nas/devices/{device.id}",
        edit_url=f"/admin/network/nas/{device.id}/edit",
    )


def _normalize_port_name(value: str | None) -> str:
    """Normalize interface/port names for loose matching."""
    if not value:
        return ""
    text = str(value).strip()
    hint = _extract_pon_hint(text)
    if hint:
        return hint.lower()
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _dedupe_live_board_inventory(
    items: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Collapse duplicate ENTITY-MIB rows that point to the same physical slot."""
    deduped: list[dict[str, object]] = []
    seen: dict[tuple[object, object], int] = {}

    for item in items:
        category = item.get("category")
        slot_number = item.get("slot_number")
        if category == "card" and slot_number is not None:
            key = (category, slot_number)
        else:
            key = (
                category,
                str(item.get("card_type") or "").strip().lower() or item.get("index"),
            )

        existing_idx = seen.get(key)
        if existing_idx is None:
            seen[key] = len(deduped)
            deduped.append(dict(item))
            continue

        existing = deduped[existing_idx]
        current_label = str(existing.get("card_type") or "")
        candidate_label = str(item.get("card_type") or "")
        if len(candidate_label) > len(current_label):
            existing["card_type"] = item.get("card_type")
        if existing.get("slot_number") is None and slot_number is not None:
            existing["slot_number"] = slot_number
        if existing.get("index") is None and item.get("index") is not None:
            existing["index"] = item.get("index")

    return deduped


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
        tx_match = re.search(
            r"tx[^-0-9]*(-?\d+(?:\.\d+)?)\s*d?bm", text, flags=re.IGNORECASE
        )
        if tx_match:
            try:
                return float(tx_match.group(1))
            except ValueError:
                continue
    return None


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


def _pon_port_display_value(pon_port: object | None, fallback: str = "-") -> str:
    """Return a concise PON label with optional description."""
    if pon_port is None:
        return fallback
    pon_number = (
        str(getattr(pon_port, "port_number", ""))
        if getattr(pon_port, "port_number", None) is not None
        else _pon_port_table_label(getattr(pon_port, "name", None))
    )
    pon_description = str(getattr(pon_port, "notes", None) or "").strip()
    if pon_description:
        return f"{pon_number} - {pon_description}"
    return str(pon_number or getattr(pon_port, "name", None) or fallback)


def _display_ont_serial(value: object) -> str:
    """Return a user-facing ONT serial, decoding Huawei hex form when possible."""
    serial = str(value or "").strip()
    if not serial:
        return ""
    if _is_synthetic_ont_serial(serial):
        return ""
    decoded = decode_huawei_hex_serial(serial)
    return decoded or serial


def _ont_display_serial(ont: object) -> str:
    """Return the best real ONT serial for UI tables.

    SNMP sync may create synthetic serial numbers before a vendor serial is
    known. Those are useful as internal unique keys, but should not be shown
    to operators as the ONT serial.
    """
    vendor_serial = _display_ont_serial(getattr(ont, "vendor_serial_number", None))
    if vendor_serial:
        return vendor_serial

    raw_serial = str(getattr(ont, "serial_number", "") or "").strip()
    if _is_synthetic_ont_serial(raw_serial):
        return ""
    return _display_ont_serial(raw_serial) or raw_serial


def _infer_ont_vendor_from_serial(serial: object) -> str:
    text = str(serial or "").strip().upper()
    if text.startswith("HWTC"):
        return "Huawei"
    if text.startswith("ZTEG"):
        return "ZTE"
    if text.startswith("ALCL"):
        return "Nokia"
    return ""


def _ont_identity_label(ont: object, display_serial_number: str) -> str:
    vendor = str(getattr(ont, "vendor", "") or "").strip()
    model = str(getattr(ont, "model", "") or "").strip()
    inferred_vendor = _infer_ont_vendor_from_serial(display_serial_number)
    parts = [vendor or inferred_vendor, model]
    label = " ".join(part for part in parts if part).strip()
    return label or display_serial_number or "Unknown ONT"


def _recent_acs_inform_by_ont_id(
    db: Session, ont_ids: Sequence[object]
) -> dict[str, datetime]:
    """Return the most recent ACS inform timestamp for each ONT."""
    if not ont_ids:
        return {}
    rows = db.execute(
        select(
            Tr069CpeDevice.ont_unit_id,
            func.max(Tr069CpeDevice.last_inform_at).label("last_inform_at"),
        )
        .where(Tr069CpeDevice.is_active.is_(True))
        .where(Tr069CpeDevice.ont_unit_id.in_(ont_ids))
        .group_by(Tr069CpeDevice.ont_unit_id)
    ).all()
    return {
        str(ont_id): last_inform_at
        for ont_id, last_inform_at in rows
        if ont_id is not None and last_inform_at is not None
    }


def _connection_request_state_by_ont_id(
    db: Session, ont_ids: Sequence[object]
) -> dict[str, dict[str, Any]]:
    """Return connection-request availability and latest tracked result per ONT."""
    if not ont_ids:
        return {}

    link_rows = db.execute(
        select(
            Tr069CpeDevice.ont_unit_id,
            Tr069CpeDevice.connection_request_url,
            Tr069CpeDevice.updated_at,
        )
        .where(Tr069CpeDevice.is_active.is_(True))
        .where(Tr069CpeDevice.ont_unit_id.in_(ont_ids))
        .order_by(Tr069CpeDevice.updated_at.desc(), Tr069CpeDevice.created_at.desc())
    ).all()

    state_by_ont_id: dict[str, dict[str, Any]] = {}
    for ont_id, connection_request_url, updated_at in link_rows:
        if ont_id is None:
            continue
        ont_key = str(ont_id)
        if ont_key in state_by_ont_id:
            continue
        has_url = bool(str(connection_request_url or "").strip())
        state_by_ont_id[ont_key] = {
            "connection_request_status": "ready" if has_url else "unavailable",
            "connection_request_display": "Ready" if has_url else "Unavailable",
            "connection_request_class": CONNECTION_REQUEST_STATUS_CLASSES[
                "ready" if has_url else "unavailable"
            ],
            "connection_request_url": str(connection_request_url or "").strip() or None,
            "connection_request_checked_at": updated_at,
            "connection_request_message": (
                "Connection request URL is available for on-demand TR-069 wakeup."
                if has_url
                else "No ConnectionRequestURL has been reported by the device yet."
            ),
        }

    op_rows = db.scalars(
        select(NetworkOperation)
        .where(NetworkOperation.target_type == NetworkOperationTargetType.ont)
        .where(NetworkOperation.target_id.in_(ont_ids))
        .where(
            NetworkOperation.operation_type
            == NetworkOperationType.ont_send_conn_request
        )
        .order_by(NetworkOperation.created_at.desc())
    ).all()

    for op in op_rows:
        ont_key = str(op.target_id)
        state = state_by_ont_id.setdefault(
            ont_key,
            {
                "connection_request_status": "unavailable",
                "connection_request_display": "Unavailable",
                "connection_request_class": CONNECTION_REQUEST_STATUS_CLASSES[
                    "unavailable"
                ],
                "connection_request_url": None,
                "connection_request_checked_at": None,
                "connection_request_message": (
                    "No ConnectionRequestURL has been reported by the device yet."
                ),
            },
        )
        if state.get("connection_request_last_attempt_at") is not None:
            continue

        status = op.status.value if op.status else "pending"
        mapped_status = {
            NetworkOperationStatus.succeeded.value: "successful",
            NetworkOperationStatus.failed.value: "failed",
            NetworkOperationStatus.running.value: "in_progress",
            NetworkOperationStatus.pending.value: "in_progress",
            NetworkOperationStatus.waiting.value: "in_progress",
            NetworkOperationStatus.canceled.value: "ready",
        }.get(status, "ready")

        state["connection_request_status"] = mapped_status
        state["connection_request_display"] = CONNECTION_REQUEST_STATUS_DISPLAY[
            mapped_status
        ]
        state["connection_request_class"] = CONNECTION_REQUEST_STATUS_CLASSES[
            mapped_status
        ]
        state["connection_request_last_attempt_at"] = op.created_at
        state["connection_request_message"] = (
            op.error
            or state.get("connection_request_message")
            or "Connection request attempted."
        )

    return state_by_ont_id


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


def _snmp_index_to_fsp(
    raw_index: str, packed_fsp_map: dict[str, str] | None = None
) -> str | None:
    """Best-effort map SNMP composite index to frame/slot/port string."""
    parts = _parse_composite_index(raw_index)
    if len(parts) >= 3:
        return f"{parts[0]}/{parts[1]}/{parts[2]}"
    if len(parts) == 2 and packed_fsp_map:
        return packed_fsp_map.get(parts[0])
    return None


def _parse_snmp_signal_dbm(
    raw_value: str | None, *, scale: float = 0.01
) -> float | None:
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


def _parse_walk_composite(
    lines: Sequence[str], *, suffix_parts: int = 4
) -> dict[str, str]:
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
            index = (
                ".".join(oid_tokens[-suffix_parts:])
                if len(oid_tokens) >= suffix_parts
                else oid_tokens[-1]
            )
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


def _build_packed_fsp_map(
    indexes: Sequence[str], pon_hints: Sequence[str]
) -> dict[str, str]:
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
    if monitoring_device is None or not getattr(
        monitoring_device, "snmp_enabled", False
    ):
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
            if isinstance(data, dict) and data.get("status") == "success":
                results = data.get("data", {}).get("result", [])
                if results and results[0].get("value"):
                    val = float(results[0]["value"][1])
                    result[key] = val
                    result["has_data"] = True
        except Exception:
            logger.debug("Skipping metric %s for OLT %s", key, olt_name, exc_info=True)
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
    from app.services.network.signal_thresholds import (
        classify_signal,
        get_signal_thresholds,
    )

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
        online_pct = (
            int(round((p_online / len(active_assignments)) * 100))
            if active_assignments
            else 0
        )
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
    onts_by_id = {str(ont.id): ont for ont in direct_onts if getattr(ont, "id", None)}
    for a in ont_assignments:
        ont = getattr(a, "ont_unit", None)
        if ont and getattr(ont, "id", None):
            onts_by_id[str(ont.id)] = ont
    onts_on_olt = list(onts_by_id.values())

    # Build signal data for each ONT
    signal_data: dict[str, dict[str, object]] = {}
    pon_port_display_by_ont_id: dict[str, str] = {}
    ont_mac_by_ont_id: dict[str, str] = {}
    # Get subscriber IDs from assignments to find related CPE devices
    # Note: Devices link to subscribers, not subscriptions (for independent OLT management)
    assignment_subscriber_ids = {
        getattr(a, "subscriber_id", None)
        for a in ont_assignments
        if getattr(a, "subscriber_id", None) is not None
    }
    cpe_mac_by_subscriber_id: dict[object, str] = {}
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
            subscriber_id = getattr(assignment, "subscriber_id", None)
            cpe_mac = None
            if subscriber_id is not None:
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
    from app.services.network.olt_monitoring_devices import (
        resolve_linked_network_device,
    )

    monitoring_resolution = resolve_linked_network_device(db, olt)
    monitoring_device = monitoring_resolution.device

    monitoring_data: dict[str, object] | None = None
    live_board_inventory: list[dict[str, object]] = []
    resolved_device_info = {
        "hostname": monitoring_device.hostname
        if monitoring_device and monitoring_device.hostname
        else olt.hostname,
        "mgmt_ip": monitoring_device.mgmt_ip
        if monitoring_device and monitoring_device.mgmt_ip
        else olt.mgmt_ip,
        "vendor": monitoring_device.vendor
        if monitoring_device and monitoring_device.vendor
        else olt.vendor,
        "model": monitoring_device.model
        if monitoring_device and monitoring_device.model
        else olt.model,
        "serial_number": monitoring_device.serial_number
        if monitoring_device and monitoring_device.serial_number
        else olt.serial_number,
        "firmware_version": olt.firmware_version,
        "software_version": olt.software_version,
        "supported_pon_types": getattr(olt, "supported_pon_types", None),
        "status": monitoring_device.status.value
        if monitoring_device and monitoring_device.status
        else ("active" if olt.is_active else "inactive"),
        "last_ping_at": monitoring_device.last_ping_at if monitoring_device else None,
        "last_snmp_at": monitoring_device.last_snmp_at if monitoring_device else None,
        "last_ping_ok": monitoring_device.last_ping_ok if monitoring_device else None,
        "last_snmp_ok": monitoring_device.last_snmp_ok if monitoring_device else None,
        "ping_enabled": bool(monitoring_device.ping_enabled)
        if monitoring_device
        else False,
        "snmp_enabled": bool(monitoring_device.snmp_enabled)
        if monitoring_device
        else False,
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

                sys_name = _parse_scalar(
                    _run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.1.5.0")
                )
                sys_descr = _parse_scalar(
                    _run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.1.1.0")
                )
                sys_object_id = _parse_scalar(
                    _run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.1.2.0")
                )
                sys_uptime = _parse_scalar(
                    _run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.1.3.0")
                )
                sys_contact = _parse_scalar(
                    _run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.1.4.0")
                )
                sys_location = _parse_scalar(
                    _run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.1.6.0")
                )
                if_number = _parse_scalar(
                    _run_snmpwalk(monitoring_device, ".1.3.6.1.2.1.2.1.0")
                )

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
                    ) or (
                        cls_code in {9}
                        and not looks_optics_like
                        and not looks_chassis_like
                    )
                    if looks_chassis_like and not slot_match:
                        looks_like_card = False
                    if not looks_like_card:
                        continue
                    live_board_inventory.append(
                        {
                            "index": idx,
                            "slot_number": int(slot_match.group(1))
                            if slot_match
                            else None,
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

    live_board_inventory = _dedupe_live_board_inventory(live_board_inventory)
    live_board_inventory.sort(
        key=lambda item: (
            0 if item.get("category") == "card" else 1,
            item.get("slot_number") is None,
            item.get("slot_number") if item.get("slot_number") is not None else 10**9,
            str(item.get("card_type") or "").lower(),
        )
    )
    live_board_cards = [
        item for item in live_board_inventory if item.get("category") == "card"
    ]
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
    for iface in (
        monitoring_data.get("pon_interfaces", [])
        if isinstance(monitoring_data, dict)
        else []
    ):
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
        ont_count_by_port_index[ont_port_index] = (
            ont_count_by_port_index.get(ont_port_index, 0) + 1
        )
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
        iface = pon_snmp_by_norm_name.get(
            _normalize_port_name(getattr(port, "name", None))
        )
        row_port_index = _extract_port_index(getattr(port, "port_number", None))
        if row_port_index is None:
            row_port_index = _extract_port_index(getattr(port, "name", None))
        assigned_total = int(ps.get("total", 0) or 0)
        fallback_total = (
            ont_count_by_port_index.get(row_port_index, 0)
            if row_port_index is not None
            else 0
        )
        resolved_total = assigned_total if assigned_total > 0 else fallback_total
        card_port = getattr(port, "olt_card_port", None)
        sfp_modules = (
            list(getattr(card_port, "sfp_modules", []) or []) if card_port else []
        )
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
            str(iface.get("description") or "").strip()
            if isinstance(iface, dict)
            else None
        )

        status_val = (
            str((iface or {}).get("status") or "").lower()
            if isinstance(iface, dict)
            else ""
        )
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
                avg_signal_dbm = (
                    ont_signal_sum_by_port_index[row_port_index] / signal_count
                )

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
                    ont for ont in ont_candidates if pon_hint in _ont_pon_hints(ont)
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

    from app.services.ipam_adapter import ipam_adapter

    ipam_scope = ipam_adapter.olt_scope_context(db, olt=olt)

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
        "monitoring_resolution": {
            "match_strategy": monitoring_resolution.match_strategy,
            "authoritative": monitoring_resolution.authoritative,
            "warning": monitoring_resolution.warning,
        },
        "monitoring_data": monitoring_data,
        "resolved_device_info": resolved_device_info,
        "resolved_pon_ports_count": resolved_pon_ports_count,
        "live_board_inventory": live_board_inventory,
        "live_board_cards": live_board_cards,
        "live_board_others": live_board_others,
        "pon_port_table_rows": pon_port_table_rows,
        "config_backups": config_backups,
        **ipam_scope,
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

ACS_STATUS_CLASSES: dict[str, str] = {
    "online": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "stale": "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    "unmanaged": "bg-sky-100 text-sky-800 dark:bg-sky-900 dark:text-sky-200",
    "unknown": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
}

CONNECTION_REQUEST_STATUS_CLASSES: dict[str, str] = {
    "successful": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "ready": "bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200",
    "in_progress": "bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200",
    "failed": "bg-rose-100 text-rose-800 dark:bg-rose-900/60 dark:text-rose-200",
    "unavailable": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
}

CONNECTION_REQUEST_STATUS_DISPLAY: dict[str, str] = {
    "successful": "Successful",
    "ready": "Ready",
    "in_progress": "In Progress",
    "failed": "Failed",
    "unavailable": "Unavailable",
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
    return bool(
        re.match(
            r"^(HW|ZT|NK|OLT)-[A-F0-9]{8}-[A-Z0-9]+(?:-\d{10,20})?$",
            text,
            re.IGNORECASE,
        )
    )


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
    authorization: str | None = None,
    offline_reason: str | None = None,
    signal_quality: str | None = None,
    search: str | None = None,
    vendor: str | None = None,
    pppoe_health: str | None = None,
    order_by: str = "serial_number",
    order_dir: str = "asc",
    page: int = 1,
    per_page: int = 50,
) -> dict[str, object]:
    """Return ONT/CPE list payload with advanced filtering and signal classification."""
    from app.models.network import OLTDevice, OntUnit
    from app.services.network.signal_thresholds import get_signal_thresholds

    normalized_view = (
        "diagnostics"
        if view == "diagnostics"
        else "unconfigured"
        if view == "unconfigured"
        else "list"
    )
    per_page = min(max(int(per_page or 50), 10), 500)

    # Determine is_active from status filter
    status_filter = (status or "all").strip().lower()
    is_active: bool | None = None
    if status_filter == "active":
        is_active = True
    elif status_filter == "inactive":
        is_active = False

    authorization_filter = (authorization or "authorized").strip().lower()
    if authorization_filter not in {"authorized", "unauthorized", "all"}:
        authorization_filter = "authorized"
    query_authorization = None if authorization_filter == "all" else authorization_filter

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
        authorization_status=query_authorization,
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
            authorization_status=query_authorization,
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
    displayed_onts = list(onts) + [
        item for item in diagnostics_onts if item not in onts
    ]
    acs_last_inform_by_ont_id = _recent_acs_inform_by_ont_id(
        db, [ont.id for ont in displayed_onts if getattr(ont, "id", None)]
    )
    connection_request_by_ont_id = _connection_request_state_by_ont_id(
        db, [ont.id for ont in displayed_onts if getattr(ont, "id", None)]
    )
    now = datetime.now(UTC)
    for ont in displayed_onts:
        quality = _classify_ont_signal(ont, warn, crit)
        acs_last_inform_at = acs_last_inform_by_ont_id.get(str(ont.id)) or getattr(
            ont, "acs_last_inform_at", None
        )
        connection_request_info = connection_request_by_ont_id.get(str(ont.id), {})
        status_snapshot = resolve_ont_status_for_model(
            ont, acs_last_inform_at=acs_last_inform_at, now=now
        )
        status_val = status_snapshot.effective_status.value
        status_display_val = None if status_val == "unknown" else status_val
        status_source = status_snapshot.effective_status_source.value
        olt_status_val = status_snapshot.olt_status.value
        olt_status_display_val = None if olt_status_val == "unknown" else olt_status_val
        acs_status_val = status_snapshot.acs_status.value
        reason = getattr(ont, "offline_reason", None)
        reason_val = reason.value if reason else None
        signal_data[str(ont.id)] = {
            "quality": quality,
            "quality_class": SIGNAL_QUALITY_CLASSES.get(
                quality, SIGNAL_QUALITY_CLASSES["unknown"]
            ),
            "status_class": ONLINE_STATUS_CLASSES.get(
                status_display_val or "unknown", ONLINE_STATUS_CLASSES["unknown"]
            ),
            "status_display": (
                status_display_val.replace("_", " ").title()
                if status_display_val
                else ""
            ),
            "status_source": status_source,
            "olt_status": olt_status_display_val,
            "olt_status_display": (
                olt_status_display_val.replace("_", " ").title()
                if olt_status_display_val
                else ""
            ),
            "olt_status_class": ONLINE_STATUS_CLASSES.get(
                olt_status_display_val or "unknown", ONLINE_STATUS_CLASSES["unknown"]
            ),
            "acs_status": acs_status_val,
            "acs_status_display": acs_status_val.replace("_", " ").title(),
            "acs_status_class": ACS_STATUS_CLASSES.get(
                acs_status_val, ACS_STATUS_CLASSES["unknown"]
            ),
            "acs_last_inform_at": acs_last_inform_at,
            "connection_request_status": connection_request_info.get(
                "connection_request_status", "unavailable"
            ),
            "connection_request_display": connection_request_info.get(
                "connection_request_display", "Unavailable"
            ),
            "connection_request_class": connection_request_info.get(
                "connection_request_class",
                CONNECTION_REQUEST_STATUS_CLASSES["unavailable"],
            ),
            "reason_display": OFFLINE_REASON_DISPLAY.get(reason_val, "")
            if reason_val and status_val == "offline"
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
        "authorization_status": query_authorization,
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
    hex_serial_by_ont_id: dict[str, str] = {}
    displayed_ont_by_id: dict[str, object] = {}
    serial_keys_by_ont_id: dict[str, str] = {}
    serial_variants: set[str] = set()
    for ont in list(onts) + [item for item in diagnostics_onts if item not in onts]:
        ont_key = str(ont.id)
        displayed_ont_by_id[ont_key] = ont
        display_serial = _ont_display_serial(ont)
        if display_serial:
            serial_display_by_ont_id[ont_key] = display_serial
            serial_keys_by_ont_id[ont_key] = display_serial.upper()
            serial_variants.add(display_serial.upper())
            # Compute hex serial for display from the real serial, not a synthetic key.
            hex_serial = encode_to_hex_serial(display_serial)
            if hex_serial:
                hex_serial_by_ont_id[ont_key] = hex_serial
                serial_variants.add(hex_serial.upper())
            raw_serial = str(getattr(ont, "serial_number", "") or "").strip()
            if raw_serial:
                serial_variants.add(raw_serial.upper())
            continue
        mac_value = str(getattr(ont, "mac_address", "") or "").strip()
        if mac_value:
            serial_display_by_ont_id[ont_key] = mac_value
        else:
            serial_display_by_ont_id[ont_key] = "-"
    if ont_ids:
        def assignment_table_info(assignment: object) -> dict[str, str]:
            pon_port = getattr(assignment, "pon_port", None)
            olt = pon_port.olt if pon_port else None
            subscriber = getattr(assignment, "subscriber", None)
            return {
                "olt_name": getattr(olt, "name", None),
                "olt_id": str(olt.id) if olt else "",
                "pon_port_name": getattr(pon_port, "name", None),
                "pon_port_display": _pon_port_display_value(pon_port),
                "subscriber_name": _subscriber_display_name(subscriber)
                if subscriber
                else "",
                "subscriber_customer_url": (
                    f"/admin/customers/business/{subscriber.id}"
                    if subscriber
                    and getattr(subscriber, "category", None)
                    == SubscriberCategory.business
                    else f"/admin/customers/person/{subscriber.id}"
                    if subscriber
                    else ""
                ),
            }

        assignment_filters = [OntAssignment.ont_unit_id.in_(ont_ids)]
        if serial_variants:
            assignment_filters.append(func.upper(OntUnit.serial_number).in_(serial_variants))
        assign_rows = db.scalars(
            select(OntAssignment)
            .join(OntUnit, OntUnit.id == OntAssignment.ont_unit_id)
            .options(
                joinedload(OntAssignment.ont_unit),
                joinedload(OntAssignment.subscriber),
                joinedload(OntAssignment.pon_port).joinedload(PonPort.olt),
            )
            .where(OntAssignment.active.is_(True))
            .where(or_(*assignment_filters))
        ).all()
        assignment_info_by_serial: dict[str, dict[str, str]] = {}
        for assignment in assign_rows:
            info = assignment_table_info(assignment)
            assigned_ont_key = str(assignment.ont_unit_id)
            if assigned_ont_key in displayed_ont_by_id:
                assignment_info[assigned_ont_key] = info
            assigned_ont = getattr(assignment, "ont_unit", None)
            assigned_serial_key = _ont_display_serial(assigned_ont).upper()
            if assigned_serial_key:
                assignment_info_by_serial[assigned_serial_key] = info

        for ont_key, serial_key in serial_keys_by_ont_id.items():
            if ont_key not in assignment_info and serial_key in assignment_info_by_serial:
                assignment_info[ont_key] = {
                    **assignment_info_by_serial[serial_key],
                    "assignment_source": "normalized_serial",
                }

        direct_pon_names = {
            fsp
            for ont in displayed_onts
            if (fsp := _normalize_ont_port_display(ont.board, ont.port))
        }
        direct_olt_ids = {
            ont.olt_device_id
            for ont in displayed_onts
            if getattr(ont, "olt_device_id", None)
        }
        direct_pon_by_key: dict[tuple[str, str], PonPort] = {}
        if direct_olt_ids and direct_pon_names:
            direct_pon_rows = db.scalars(
                select(PonPort)
                .options(joinedload(PonPort.olt))
                .where(PonPort.olt_id.in_(direct_olt_ids))
                .where(PonPort.name.in_(direct_pon_names))
            ).all()
            direct_pon_by_key = {
                (str(row.olt_id), str(row.name)): row for row in direct_pon_rows
            }

        for ont in displayed_onts:
            ont_key = str(getattr(ont, "id", ""))
            if not ont_key:
                continue
            olt = getattr(ont, "olt_device", None)
            ont_olt_id = str(
                getattr(ont, "olt_device_id", None) or getattr(olt, "id", "") or ""
            )
            if not ont_olt_id:
                continue
            pon_fsp = _normalize_ont_port_display(ont.board, ont.port)
            pon_port = direct_pon_by_key.get((ont_olt_id, pon_fsp or ""))
            existing_info = assignment_info.get(ont_key, {})
            assignment_info[ont_key] = {
                **existing_info,
                "olt_name": str(getattr(olt, "name", None) or ""),
                "olt_id": ont_olt_id,
                "pon_port_name": str(getattr(pon_port, "name", None) or pon_fsp or ""),
                "pon_port_display": _pon_port_display_value(pon_port, pon_fsp or "-"),
                "subscriber_name": existing_info.get("subscriber_name", ""),
                "subscriber_customer_url": existing_info.get(
                    "subscriber_customer_url", ""
                ),
                "topology_source": "ont",
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

    # Build profile info lookup for displayed ONTs
    from app.models.network import OntProvisioningProfile

    profile_info: dict[str, dict[str, str]] = {}
    active_assignments = {
        str(getattr(ont, "id", "")): get_active_bundle_assignment(db, ont)
        for ont in displayed_onts
        if getattr(ont, "id", None)
    }
    profile_ids = {
        assignment.bundle_id
        for assignment in active_assignments.values()
        if assignment and getattr(assignment, "bundle_id", None)
    }
    profile_by_id: dict[str, OntProvisioningProfile] = {}
    if profile_ids:
        profile_rows = db.scalars(
            select(OntProvisioningProfile).where(
                OntProvisioningProfile.id.in_(profile_ids)
            )
        ).all()
        profile_by_id = {str(p.id): p for p in profile_rows}
    for ont in displayed_onts:
        ont_key = str(getattr(ont, "id", ""))
        if not ont_key:
            continue
        assignment = active_assignments.get(ont_key)
        profile_id = getattr(assignment, "bundle_id", None)
        if profile_id and str(profile_id) in profile_by_id:
            profile = profile_by_id[str(profile_id)]
            profile_info[ont_key] = {
                "profile_id": str(profile.id),
                "profile_name": profile.name or "",
                "profile_type": profile.profile_type.value if profile.profile_type else "",
            }

    return {
        "onts": onts,
        "diagnostics_onts": diagnostics_onts,
        "stats": stats,
        "status_filter": status_filter,
        "signal_data": signal_data,
        "assignment_info": assignment_info,
        "profile_info": profile_info,
        "serial_display_by_ont_id": serial_display_by_ont_id,
        "hex_serial_by_ont_id": hex_serial_by_ont_id,
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
            "authorization": authorization_filter,
            "signal_quality": signal_quality or "",
            "search": search or "",
            "vendor": vendor or "",
            "view": normalized_view,
            "order_by": order_by,
            "order_dir": order_dir,
            "per_page": per_page,
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
    from app.services.network.signal_thresholds import (
        classify_signal,
        get_signal_thresholds,
        normalize_optical_signal_dbm,
    )

    warn, crit = get_signal_thresholds(db)
    olt_rx = normalize_optical_signal_dbm(getattr(ont, "olt_rx_signal_dbm", None))
    onu_rx = normalize_optical_signal_dbm(getattr(ont, "onu_rx_signal_dbm", None))
    olt_quality = classify_signal(olt_rx, warn_threshold=warn, crit_threshold=crit)
    onu_quality = classify_signal(onu_rx, warn_threshold=warn, crit_threshold=crit)
    acs_last_inform_at = _recent_acs_inform_by_ont_id(db, [ont.id]).get(
        str(ont.id)
    ) or getattr(ont, "acs_last_inform_at", None)
    connection_request_info = _connection_request_state_by_ont_id(db, [ont.id]).get(
        str(ont.id), {}
    )
    status_snapshot = resolve_ont_status_for_model(
        ont, acs_last_inform_at=acs_last_inform_at
    )
    status_val = status_snapshot.effective_status.value
    status_display_val = None if status_val == "unknown" else status_val
    status_source = status_snapshot.effective_status_source.value
    olt_status_val = status_snapshot.olt_status.value
    olt_status_display_val = None if olt_status_val == "unknown" else olt_status_val
    acs_status_val = status_snapshot.acs_status.value
    normalized_acs_last_inform_at = status_snapshot.acs_last_inform_at
    reason = getattr(ont, "offline_reason", None)
    reason_val = reason.value if reason else None
    effective_last_seen_at = resolve_effective_last_seen_at(
        ont, acs_last_inform_at=normalized_acs_last_inform_at
    )

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
        "online_status": status_display_val,
        "online_status_display": (
            status_display_val.replace("_", " ").title() if status_display_val else ""
        ),
        "online_status_class": ONLINE_STATUS_CLASSES.get(
            status_display_val or "unknown", ONLINE_STATUS_CLASSES["unknown"]
        ),
        "online_status_source": status_source,
        "olt_status": olt_status_display_val,
        "olt_status_display": (
            olt_status_display_val.replace("_", " ").title()
            if olt_status_display_val
            else ""
        ),
        "olt_status_class": ONLINE_STATUS_CLASSES.get(
            olt_status_display_val or "unknown", ONLINE_STATUS_CLASSES["unknown"]
        ),
        "acs_status": acs_status_val,
        "acs_status_class": ACS_STATUS_CLASSES.get(
            acs_status_val, ACS_STATUS_CLASSES["unknown"]
        ),
        "last_seen_at": effective_last_seen_at,
        "acs_last_inform_at": normalized_acs_last_inform_at,
        "connection_request_status": connection_request_info.get(
            "connection_request_status", "unavailable"
        ),
        "connection_request_display": connection_request_info.get(
            "connection_request_display", "Unavailable"
        ),
        "connection_request_class": connection_request_info.get(
            "connection_request_class",
            CONNECTION_REQUEST_STATUS_CLASSES["unavailable"],
        ),
        "connection_request_url": connection_request_info.get("connection_request_url"),
        "connection_request_last_attempt_at": connection_request_info.get(
            "connection_request_last_attempt_at"
        ),
        "connection_request_message": connection_request_info.get(
            "connection_request_message"
        ),
        "offline_reason": reason_val,
        "offline_reason_display": OFFLINE_REASON_DISPLAY.get(reason_val, "")
        if reason_val and status_val == "offline"
        else "",
        "warn_threshold": warn,
        "crit_threshold": crit,
    }

    display_serial_number = _ont_display_serial(ont)
    display_serial_label = display_serial_number or "-"
    identity_label = _ont_identity_label(ont, display_serial_number)

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
                    network_path["splitter_name"] = (
                        sp.splitter.name or str(sp.splitter.id)[:8]
                    )

    # Subscriber and subscription info
    subscriber_info: dict[str, object] = {}
    if assignment and assignment.subscriber:
        sub = assignment.subscriber
        subscriber_info["id"] = str(sub.id)
        subscriber_info["customer_url"] = (
            f"/admin/customers/business/{sub.id}"
            if getattr(sub, "category", None) == SubscriberCategory.business
            else f"/admin/customers/person/{sub.id}"
        )
        subscriber_info["name"] = _subscriber_display_name(sub)
        subscriber_info["status"] = sub.status.value if sub.status else "unknown"
        subscriber_info["status_class"] = SUBSCRIBER_STATUS_CLASSES.get(
            str(subscriber_info["status"]),
            SUBSCRIBER_STATUS_CLASSES["unknown"],
        )
    # Look up active subscription for subscriber (no longer directly on assignment)
    subscription = None
    if assignment and assignment.subscriber_id:
        subscription_stmt = (
            select(Subscription)
            .where(
                Subscription.subscriber_id == assignment.subscriber_id,
                Subscription.status == SubscriptionStatus.active,
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        subscription = db.scalars(subscription_stmt).first()

    if subscription:
        subscriber_info["subscription_id"] = str(subscription.id)
        subscriber_info["plan_name"] = (
            subscription.offer.name
            if hasattr(subscription, "offer") and subscription.offer
            else None
        )
        subscriber_info["subscription_status"] = (
            subscription.status.value if subscription.status else "unknown"
        )

    provisioning_runs: list[dict[str, object]] = []
    ont_plan: dict[str, object] = {}
    subscription_entity_id = subscription.id if subscription else None
    if subscription_entity_id is not None:
        from app.models.provisioning import ProvisioningRun, ServiceOrder

        run_stmt = (
            select(ProvisioningRun)
            .options(joinedload(ProvisioningRun.workflow))
            .where(ProvisioningRun.subscription_id == subscription_entity_id)
            .order_by(ProvisioningRun.created_at.desc())
            .limit(10)
        )
        recent_runs = list(db.scalars(run_stmt).all())
        for run in recent_runs:
            results = []
            if isinstance(run.output_payload, dict):
                maybe_results = run.output_payload.get("results")
                if isinstance(maybe_results, list):
                    results = [item for item in maybe_results if isinstance(item, dict)]
            provisioning_runs.append(
                {
                    "id": str(run.id),
                    "workflow_name": (
                        run.workflow.name
                        if getattr(run, "workflow", None) is not None
                        else None
                    ),
                    "status": run.status.value if run.status else "unknown",
                    "created_at": run.created_at,
                    "completed_at": run.completed_at,
                    "step_count": len(results),
                    "success_count": sum(
                        1 for item in results if item.get("status") == "success"
                    ),
                    "error_message": run.error_message,
                }
            )
        order_stmt = (
            select(ServiceOrder)
            .where(ServiceOrder.subscription_id == subscription_entity_id)
            .order_by(ServiceOrder.created_at.desc())
            .limit(1)
        )
        service_order = db.scalars(order_stmt).first()
        execution_context = getattr(service_order, "execution_context", None) or {}
        if isinstance(execution_context, dict):
            maybe_ont_plan = execution_context.get("ont_plan")
            if isinstance(maybe_ont_plan, dict):
                ont_plan = maybe_ont_plan

    from app.models.compensation_failure import CompensationFailure
    from app.services.network.compensation_retry import is_retry_due, next_retry_at

    compensation_stmt = (
        select(CompensationFailure)
        .where(CompensationFailure.ont_unit_id == ont.id)
        .order_by(CompensationFailure.created_at.desc())
        .limit(10)
    )
    compensation_failures = []
    for failure in db.scalars(compensation_stmt).all():
        next_attempt_at = next_retry_at(failure)
        compensation_failures.append(
            {
                "id": str(failure.id),
                "status": failure.status.value,
                "step_name": failure.step_name,
                "operation_type": failure.operation_type,
                "description": failure.description,
                "error_message": failure.error_message,
                "failure_count": failure.failure_count,
                "created_at": failure.created_at,
                "last_attempted_at": failure.last_attempted_at,
                "next_retry_at": next_attempt_at,
                "retry_due": is_retry_due(failure),
                "resolved_at": failure.resolved_at,
                "resolved_by": failure.resolved_by,
                "resource_id": failure.resource_id,
            }
        )

    # Manual profile state shown on the ONT detail screen
    profile_state: dict[str, object] = {}
    active_assignment = get_active_bundle_assignment(db, ont)
    active_profile_id = getattr(active_assignment, "bundle_id", None)
    if active_profile_id:
        profile_state["profile_id"] = str(active_profile_id)
        profile_state["status"] = (
            ont.provisioning_status.value if ont.provisioning_status else None
        )
        profile_state["last_provisioned_at"] = ont.last_provisioned_at
        bundle = getattr(active_assignment, "bundle", None)
        if bundle is not None:
            profile_state["profile_name"] = bundle.name

        # Check for drift
        from app.services.network.ont_profile_apply import detect_drift

        drift = detect_drift(db, str(ont.id))
        if drift:
            profile_state["has_drift"] = drift.has_drift
            profile_state["drifted_fields"] = [
                {
                    "field": f.field_name,
                    "desired": str(f.desired),
                    "observed": str(f.observed),
                }
                for f in drift.drifted_fields
            ]

    # Note: available_profile_templates and available_firmware are now lazy-loaded
    # via HTMX endpoints to reduce initial page load time

    from app.services.service_intent_ui_adapter import service_intent_ui_adapter

    capabilities = service_intent_ui_adapter.ont_capabilities(db, ont_id=ont_id)
    service_intent = service_intent_ui_adapter.build_ont_service_intent(
        ont,
        db=db,
        subscriber_info=subscriber_info,
        ont_plan=ont_plan,
    )
    try:
        from app.services.network.ont_tr069 import OntTR069

        cached_summary = OntTR069._summary_from_snapshot(ont)
        acs_observed_intent = service_intent_ui_adapter.build_acs_observed_service_intent(
            cached_summary
        )
    except Exception:
        logger.exception(
            "Failed to load cached ACS observed service intent for ONT %s", ont_id
        )
        acs_observed_intent = service_intent_ui_adapter.build_acs_observed_service_intent(
            None
        )
    observed_runtime_summary = _acs_observed_runtime_summary(
        acs_observed_intent,
        db=db,
        ont=ont,
    )
    last_config_summary = _ont_last_config_summary(
        ont,
        acs_observed_intent=acs_observed_intent,
    )
    desired_config_summary = _ont_desired_config_summary(db, ont, ont_plan=ont_plan)
    connected_wifi_clients = observed_runtime_summary.get("wifi_clients")
    connected_customer_devices = observed_runtime_summary.get("customer_devices")

    # Configure form context (VLANs for dropdowns)
    from app.services import web_network_onts as web_network_onts_service

    configure_vlans = web_network_onts_service.get_vlans_for_ont(db, ont)
    configure_mgmt_ip_choices = web_network_onts_service.management_ip_choices_for_ont(
        db, ont
    )

    return {
        "ont": ont,
        "display_serial_number": display_serial_number,
        "display_serial_label": display_serial_label,
        "identity_label": identity_label,
        "assignment": assignment,
        "past_assignments": past_assignments,
        "signal_info": signal_info,
        "network_path": network_path,
        "subscriber_info": subscriber_info,
        "provisioning_runs": provisioning_runs,
        "compensation_failures": compensation_failures,
        "ont_plan": ont_plan,
        "service_intent": service_intent,
        "acs_observed_intent": acs_observed_intent,
        "observed_runtime_summary": observed_runtime_summary,
        "last_config_summary": last_config_summary,
        "desired_config_summary": desired_config_summary,
        "connected_customer_devices": connected_customer_devices,
        "connected_wifi_clients": connected_wifi_clients,
        "profile_state": profile_state,
        "capabilities": capabilities,
        "inventory_ready": (
            not bool(assignment)
            and not bool(getattr(ont, "external_id", None))
            and not bool(profile_state.get("profile_id"))
            and profile_state.get("status") in (None, "unprovisioned")
        ),
        # Configure form context
        "configure_vlans": configure_vlans,
        **configure_mgmt_ip_choices,
    }


def _safe_int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _acs_observed_runtime_summary(
    acs_observed_intent: dict[str, object], *, db: Session, ont: object
) -> dict[str, object]:
    effective = resolve_effective_ont_config(db, ont)
    values = effective["values"]
    tracked_index = acs_observed_intent.get("tracked_point_index", {})
    tracked_index = tracked_index if isinstance(tracked_index, dict) else {}

    def tracked_raw(key: str) -> object | None:
        point = tracked_index.get(key)
        if not isinstance(point, dict):
            return None
        return point.get("raw_value")

    observed = acs_observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    lan_hosts = observed.get("lan_hosts", [])
    lan_hosts = lan_hosts if isinstance(lan_hosts, list) else []

    wifi_clients = _safe_int(tracked_raw("wifi.connected_clients"))
    if wifi_clients is None:
        wifi_clients = _safe_int(getattr(ont, "observed_wifi_clients", None))

    customer_devices = None
    if lan_hosts:
        customer_devices = _lan_host_connected_count(lan_hosts)
    if customer_devices is None:
        customer_devices = _safe_int(tracked_raw("lan.connected_hosts"))
    if customer_devices is None:
        customer_devices = _safe_int(getattr(ont, "observed_lan_hosts", None))
    if customer_devices is None:
        customer_devices = wifi_clients
    fetched_at = acs_observed_intent.get("fetched_at")
    updated_at_display = "-"
    if isinstance(fetched_at, datetime):
        updated_at_display = fetched_at.strftime("%Y-%m-%d %H:%M")
    elif fetched_at:
        updated_at_display = str(fetched_at)
    has_runtime = bool(
        acs_observed_intent.get("available")
        or tracked_raw("system.mac_address")
        or tracked_raw("wan.wan_ip")
        or tracked_raw("wan.pppoe_username")
        or tracked_raw("wan.status")
        or tracked_raw("lan.lan_ip")
        or wifi_clients is not None
        or customer_devices is not None
        or fetched_at
    )

    return {
        "has_runtime": has_runtime,
        "mac_address": tracked_raw("system.mac_address") or getattr(ont, "mac_address", None),
        "wan_ip": tracked_raw("wan.wan_ip"),
        "pppoe_user": tracked_raw("wan.pppoe_username") or values.get("pppoe_username"),
        "pppoe_status": tracked_raw("wan.status"),
        "wan_mode": values.get("wan_mode"),
        "lan_mode": tracked_raw("lan.dhcp_enabled"),
        "lan_ip": tracked_raw("lan.lan_ip"),
        "wifi_clients": wifi_clients,
        "customer_devices": customer_devices,
        "updated_at": fetched_at,
        "updated_at_display": updated_at_display,
    }


def _lan_host_connected_count(hosts: object) -> int | None:
    """Return the best known customer-device count behind an ONT."""
    if not isinstance(hosts, list):
        return None
    connected = 0
    for host in hosts:
        if not isinstance(host, dict):
            continue
        active = host.get("active")
        active_text = str(active).strip().lower()
        if active_text in {"false", "0", "no", "inactive", "down"}:
            continue
        if not any(
            str(host.get(key) or "").strip()
            for key in (
                "host_name",
                "ip_address",
                "mac_address",
                "HostName",
                "IPAddress",
                "MACAddress",
            )
        ):
            continue
        connected += 1
    return connected


def _active_ethernet_port_count(ports: object) -> tuple[int | None, int | None]:
    if not isinstance(ports, list):
        return None, None
    active = 0
    for port in ports:
        if not isinstance(port, dict):
            continue
        status = str(port.get("link_status") or port.get("Status") or "").strip().lower()
        if status == "up":
            active += 1
    return active, len(ports)


def _display_bool(value: object) -> str:
    if value is True:
        return "Enabled"
    if value is False:
        return "Disabled"
    return str(value or "")


def _format_observed_time(value: object) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if value:
        return str(value)
    return "unknown time"


def _ont_last_config_summary(
    ont: object,
    *,
    acs_observed_intent: dict[str, object],
) -> dict[str, object]:
    ont_id = str(getattr(ont, "id", ""))
    empty_summary = {
        "has_snapshot": False,
        "empty_message": "No cached config state yet.",
        "empty_hint": "Query the ONT to fetch and cache the latest TR-069 state.",
        "fetched_at": None,
        "fetched_at_display": "never",
        "source": "TR-069 cache",
        "manufacturer": "",
        "firmware": "",
        "wan_ip": "",
        "wan_status": "",
        "pppoe_user": "",
        "lan_ip": "",
        "dhcp_enabled": "",
        "ssid": "",
        "wifi_clients": None,
        "lan_hosts": None,
        "ethernet_ports": None,
        "active_ports": None,
        "metrics": [],
        "details": [],
        "configure_url": f"/admin/network/onts/{ont_id}?tab=device-config",
        "query_url": f"/admin/network/onts/{ont_id}?tab=diagnostics",
    }
    tracked_index = acs_observed_intent.get("tracked_point_index", {})
    tracked_index = tracked_index if isinstance(tracked_index, dict) else {}

    def tracked_raw(key: str) -> object | None:
        point = tracked_index.get(key)
        if not isinstance(point, dict):
            return None
        return point.get("raw_value")

    observed = acs_observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    ethernet_ports = observed.get("ethernet_ports", [])
    lan_hosts = observed.get("lan_hosts", [])

    fetched_at = acs_observed_intent.get("fetched_at")
    has_snapshot = bool(
        acs_observed_intent.get("available")
        or fetched_at
        or tracked_index
        or ethernet_ports
        or lan_hosts
    )
    if not has_snapshot:
        return empty_summary

    wifi_clients = _safe_int(tracked_raw("wifi.connected_clients"))
    lan_host_count = _lan_host_connected_count(lan_hosts)
    if lan_host_count is None:
        lan_host_count = _safe_int(tracked_raw("lan.connected_hosts"))
    active_ports, port_count = _active_ethernet_port_count(ethernet_ports)

    wan_ip = str(tracked_raw("wan.wan_ip") or "").strip()
    wan_status = str(tracked_raw("wan.status") or "").strip()
    pppoe_user = str(tracked_raw("wan.pppoe_username") or "").strip()
    lan_ip = str(tracked_raw("lan.lan_ip") or "").strip()
    dhcp_enabled = _display_bool(tracked_raw("lan.dhcp_enabled"))
    ssid = str(tracked_raw("wifi.ssid") or "").strip()
    metrics = [
        {
            "label": "WAN IP",
            "value": wan_ip or "-",
            "value_class": "font-mono",
        },
        {
            "label": "WAN status",
            "value": wan_status or "-",
            "value_class": "",
        },
        {
            "label": "SSID",
            "value": ssid or "-",
            "value_class": "font-mono",
        },
        {
            "label": "LAN hosts",
            "value": str(lan_host_count) if lan_host_count is not None else "-",
            "value_class": "",
        },
    ]
    details = [
        {
            "label": "PPPoE user",
            "value": pppoe_user or "-",
            "value_class": "font-mono",
        },
        {
            "label": "LAN gateway",
            "value": lan_ip or "-",
            "value_class": "font-mono",
        },
        {
            "label": "DHCP",
            "value": dhcp_enabled or "-",
            "value_class": "",
        },
        {
            "label": "WiFi clients",
            "value": str(wifi_clients) if wifi_clients is not None else "-",
            "value_class": "",
        },
        {
            "label": "Ethernet ports",
            "value": (
                f"{active_ports}/{port_count} up"
                if active_ports is not None and port_count is not None
                else str(port_count)
                if port_count is not None
                else "-"
            ),
            "value_class": "",
        },
    ]

    return {
        "has_snapshot": True,
        "empty_message": "",
        "empty_hint": "",
        "fetched_at": fetched_at if isinstance(fetched_at, datetime) else None,
        "fetched_at_display": _format_observed_time(fetched_at),
        "source": str(acs_observed_intent.get("source") or "TR-069 cache"),
        "manufacturer": str(tracked_raw("system.manufacturer") or ""),
        "firmware": str(tracked_raw("system.firmware") or ""),
        "wan_ip": wan_ip,
        "wan_status": wan_status,
        "pppoe_user": pppoe_user,
        "lan_ip": lan_ip,
        "dhcp_enabled": dhcp_enabled,
        "ssid": ssid,
        "wifi_clients": wifi_clients,
        "lan_hosts": lan_host_count,
        "ethernet_ports": port_count,
        "active_ports": active_ports,
        "metrics": metrics,
        "details": details,
        "configure_url": f"/admin/network/onts/{ont_id}?tab=device-config",
        "query_url": f"/admin/network/onts/{ont_id}?tab=diagnostics",
    }


def _display_config_value(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value or "").strip()
    return text or "-"


def _secret_status(value: object) -> str:
    return "Set (hidden)" if str(value or "").strip() else "Not set"


def _enum_or_text(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def _ont_desired_config_summary(
    db: Session,
    ont: object,
    *,
    ont_plan: dict[str, object],
) -> dict[str, object]:
    """Return DB-backed intended config for the overview page."""
    plan = ont_plan if isinstance(ont_plan, dict) else {}
    mgmt = plan.get("configure_management_ip")
    mgmt = mgmt if isinstance(mgmt, dict) else {}
    wan = plan.get("configure_wan_tr069")
    wan = wan if isinstance(wan, dict) else {}
    pppoe = plan.get("push_pppoe_tr069") or plan.get("push_pppoe_omci")
    pppoe = pppoe if isinstance(pppoe, dict) else {}
    lan = plan.get("configure_lan_tr069")
    lan = lan if isinstance(lan, dict) else {}
    wifi = plan.get("configure_wifi_tr069")
    wifi = wifi if isinstance(wifi, dict) else {}
    olt_snapshot = getattr(ont, "olt_observed_snapshot", None)
    olt_snapshot = olt_snapshot if isinstance(olt_snapshot, dict) else {}
    iphost = olt_snapshot.get("iphost_config")
    iphost = iphost if isinstance(iphost, dict) else {}
    effective = resolve_effective_ont_config(db, ont)
    values = effective["values"]

    mgmt_vlan = values.get("mgmt_vlan")
    wan_vlan = values.get("wan_vlan")
    rows = [
        {
            "label": "Mgmt mode",
            "value": _display_config_value(
                values.get("mgmt_ip_mode") or mgmt.get("ip_mode")
            ),
            "value_class": "",
        },
        {
            "label": "Mgmt VLAN",
            "value": _display_config_value(mgmt_vlan or mgmt.get("vlan_id")),
            "value_class": "font-mono",
        },
        {
            "label": "Mgmt IP",
            "value": _display_config_value(
                values.get("mgmt_ip_address") or mgmt.get("ip_address")
            ),
            "value_class": "font-mono",
        },
        {
            "label": "Observed IPHOST",
            "value": _display_config_value(
                iphost.get("IP Address")
                or iphost.get("IP")
                or iphost.get("ip_address")
            ),
            "value_class": "font-mono",
        },
        {
            "label": "TR-069 profile",
            "value": _display_config_value(getattr(ont, "tr069_olt_profile_id", None)),
            "value_class": "font-mono",
        },
        {
            "label": "WAN mode",
            "value": _display_config_value(
                values.get("wan_mode") or wan.get("wan_mode")
            ),
            "value_class": "",
        },
        {
            "label": "WAN VLAN",
            "value": _display_config_value(
                wan_vlan or wan.get("wan_vlan") or pppoe.get("vlan_id")
            ),
            "value_class": "font-mono",
        },
        {
            "label": "PPPoE user",
            "value": _display_config_value(
                values.get("pppoe_username") or pppoe.get("username")
            ),
            "value_class": "font-mono",
        },
        {
            "label": "PPPoE password",
            "value": _secret_status(getattr(ont, "pppoe_password", None)),
            "value_class": "",
        },
        {
            "label": "LAN gateway",
            "value": _display_config_value(
                getattr(ont, "lan_gateway_ip", None) or lan.get("lan_ip")
            ),
            "value_class": "font-mono",
        },
        {
            "label": "LAN subnet",
            "value": _display_config_value(
                getattr(ont, "lan_subnet_mask", None) or lan.get("lan_subnet")
            ),
            "value_class": "font-mono",
        },
        {
            "label": "WiFi enabled",
            "value": _display_config_value(
                values.get("wifi_enabled")
                if values.get("wifi_enabled") is not None
                else wifi.get("enabled")
            ),
            "value_class": "",
        },
        {
            "label": "SSID",
            "value": _display_config_value(values.get("wifi_ssid") or wifi.get("ssid")),
            "value_class": "font-mono",
        },
        {
            "label": "WiFi password",
            "value": _secret_status(getattr(ont, "wifi_password", None)),
            "value_class": "",
        },
        {
            "label": "WiFi channel",
            "value": _display_config_value(
                values.get("wifi_channel") or wifi.get("channel")
            ),
            "value_class": "",
        },
    ]
    return {
        "rows": rows,
        "configured_count": sum(1 for row in rows if row["value"] != "-"),
        "configure_url": f"/admin/network/onts/{getattr(ont, 'id', '')}?tab=device-config",
    }


def _subscriber_display_name(subscriber: object) -> str:
    """Build display name from subscriber fields and person fallback."""
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

    all_monitoring_devices = list(
        db.scalars(select(NetworkDevice).order_by(NetworkDevice.name)).all()
    )
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
    core_device_keys = {
        (
            str(getattr(device, "mgmt_ip", "") or "").strip(),
            str(getattr(device, "hostname", "") or "").strip(),
            str(getattr(device, "name", "") or "").strip(),
        )
        for device in core_devices
    }
    nas_devices = list(
        db.scalars(
            select(NasDevice)
            .where(NasDevice.is_active.is_(True))
            .order_by(NasDevice.name.asc())
        ).all()
    )
    for nas_device in nas_devices:
        nas_stub = _nas_inventory_stub(nas_device)
        key = (
            str(getattr(nas_stub, "mgmt_ip", "") or "").strip(),
            str(getattr(nas_stub, "hostname", "") or "").strip(),
            str(getattr(nas_stub, "name", "") or "").strip(),
        )
        if key in promoted_olt_keys or key in core_device_keys:
            continue
        core_devices.append(nas_stub)
        core_device_keys.add(key)
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
    olts = sorted(
        olts_by_id.values(), key=lambda olt: str(getattr(olt, "name", "") or "").lower()
    )
    monitoring_devices = all_monitoring_devices
    by_mgmt_ip = {d.mgmt_ip: d for d in monitoring_devices if d.mgmt_ip}
    by_hostname = {d.hostname: d for d in monitoring_devices if d.hostname}
    by_name = {d.name: d for d in monitoring_devices if d.name}
    monitoring_device_ids = [d.id for d in monitoring_devices]
    interfaces_by_device_id: dict[str, list[DeviceInterface]] = {}
    if monitoring_device_ids:
        iface_rows = list(
            db.scalars(
                select(DeviceInterface).where(
                    DeviceInterface.device_id.in_(monitoring_device_ids)
                )
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
                    _display_ont_serial(getattr(ont, "serial_number", None)),
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
    serial_display_by_ont_id = {
        str(ont.id): _ont_display_serial(ont)
        for ont in onts
        if getattr(ont, "id", None)
    }
    return {
        "tab": tab,
        "search": search or "",
        "stats": stats,
        "core_devices": core_devices,
        "olts": olts,
        "olt_stats": olt_stats,
        "onts": onts,
        "serial_display_by_ont_id": serial_display_by_ont_id,
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

    nas_devices = list(
        db.scalars(select(NasDevice).order_by(NasDevice.name.asc())).all()
    )
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
                "vendor": device.vendor.value
                if getattr(device, "vendor", None)
                else None,
                "model": device.model,
                "ip_address": device.management_ip or device.ip_address or "-",
                "port": device.management_port or "-",
                "last_backup_at": last_backup_at,
                "last_message": last_message,
                "backup_status": backup_status,
                "device_url": f"/admin/network/nas/devices/{device.id}",
                "backup_url": f"/admin/network/nas/backups/{latest.id}"
                if latest
                else None,
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
            if term
            in " ".join(
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
        "status_filter": status_filter
        if status_filter in {"success", "stale", "failed"}
        else "all",
        "device_type_filter": device_type_filter
        if device_type_filter in {"nas", "olt"}
        else "all",
        "search_filter": search or "",
        "stale_hours": max(stale_hours, 1),
        "sort_filter": sort
        if sort in {"last_backup_asc", "last_backup_desc"}
        else "last_backup_asc",
    }
