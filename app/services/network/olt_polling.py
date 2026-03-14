"""OLT optical signal polling service.

Polls OLT devices via SNMP to collect per-ONT optical signal levels,
online status, and distance estimates. Updates OntUnit records in bulk.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntUnit,
    OnuOfflineReason,
    OnuOnlineStatus,
    PonPort,
)
from app.models.network_monitoring import NetworkDevice
from app.services.credential_crypto import decrypt_credential

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SNMP OID tables for per-vendor ONT optical signal polling
# ---------------------------------------------------------------------------
# Each vendor uses different OID subtrees to expose per-ONU signal data.
# The OIDs below are walked from the OLT and return per-ONU index values.

_VENDOR_OID_OIDS: dict[str, dict[str, str]] = {
    "huawei": {
        # hwGponOltOpticsDdmInfoRxPower — OLT receive power per ONU
        "olt_rx": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4",
        # hwGponOltOpticsDdmInfoTxPower — ONU receive (reported via OLT)
        "onu_rx": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.6",
        # hwGponOltEponOnuDistance
        "distance": ".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.20",
        # hwGponDeviceOnuRunStatus
        "status": ".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.15",
    },
    "zte": {
        "olt_rx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.2",
        "onu_rx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.3",
        "distance": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.7",
        "status": ".1.3.6.1.4.1.3902.1082.500.10.2.2.1.1.10",
    },
    "nokia": {
        "olt_rx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.2",
        "onu_rx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.4",
        "distance": ".1.3.6.1.4.1.637.61.1.35.10.1.1.9",
        "status": ".1.3.6.1.4.1.637.61.1.35.10.1.1.8",
    },
}

# Signal values are in 0.01 dBm units for most vendors
_VENDOR_SIGNAL_SCALE: dict[str, float] = {
    "huawei": 0.01,
    "zte": 0.01,
    "nokia": 0.01,
}

# Sentinel values commonly used by vendors to indicate invalid/unavailable optics.
_SIGNAL_SENTINELS: set[int] = {
    2147483647,
    2147483646,
    65535,
    65534,
    32767,
    -2147483648,
}

# Generic/fallback OIDs (ITU-T G.988 standard GPON MIB)
GENERIC_OIDS: dict[str, str] = {
    "olt_rx": ".1.3.6.1.4.1.17409.2.3.6.10.1.2",
    "onu_rx": ".1.3.6.1.4.1.17409.2.3.6.10.1.3",
    "distance": ".1.3.6.1.4.1.17409.2.3.6.1.1.9",
    "status": ".1.3.6.1.4.1.17409.2.3.6.1.1.8",
}


# ---------------------------------------------------------------------------
# Signal threshold classification
# ---------------------------------------------------------------------------

# Default thresholds — overridden by settings at runtime
DEFAULT_WARN_THRESHOLD = -25.0  # dBm
DEFAULT_CRIT_THRESHOLD = -28.0  # dBm

SIGNAL_QUALITY_GOOD = "good"
SIGNAL_QUALITY_WARNING = "warning"
SIGNAL_QUALITY_CRITICAL = "critical"
SIGNAL_QUALITY_UNKNOWN = "unknown"


def classify_signal(
    dbm: float | None,
    *,
    warn_threshold: float = DEFAULT_WARN_THRESHOLD,
    crit_threshold: float = DEFAULT_CRIT_THRESHOLD,
) -> str:
    """Classify optical signal quality based on dBm value.

    Args:
        dbm: Optical power in dBm (negative values).
        warn_threshold: dBm value below which signal is 'warning'.
        crit_threshold: dBm value below which signal is 'critical'.

    Returns:
        One of: 'good', 'warning', 'critical', 'unknown'.
    """
    if dbm is None:
        return SIGNAL_QUALITY_UNKNOWN
    if dbm >= warn_threshold:
        return SIGNAL_QUALITY_GOOD
    if dbm >= crit_threshold:
        return SIGNAL_QUALITY_WARNING
    return SIGNAL_QUALITY_CRITICAL


def get_signal_thresholds(db: Session) -> tuple[float, float]:
    """Load signal thresholds from settings, falling back to defaults."""
    try:
        from app.models.domain_settings import SettingDomain
        from app.services.settings_spec import resolve_value

        warn_raw = resolve_value(
            db, SettingDomain.network_monitoring, "ont_signal_warning_dbm"
        )
        crit_raw = resolve_value(
            db, SettingDomain.network_monitoring, "ont_signal_critical_dbm"
        )
        warn = float(str(warn_raw)) if warn_raw is not None else DEFAULT_WARN_THRESHOLD
        crit = float(str(crit_raw)) if crit_raw is not None else DEFAULT_CRIT_THRESHOLD
        return warn, crit
    except Exception as exc:
        logger.warning("Failed to load signal thresholds, using defaults: %s", exc)
        return DEFAULT_WARN_THRESHOLD, DEFAULT_CRIT_THRESHOLD


# ---------------------------------------------------------------------------
# SNMP helpers (reuse snmp_discovery subprocess pattern)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OntSignalReading:
    """Signal reading for a single ONT from SNMP poll."""

    onu_index: str
    olt_rx_dbm: float | None
    onu_rx_dbm: float | None
    distance_m: int | None
    is_online: bool | None


def _split_onu_index(raw_index: str) -> tuple[str, ...] | None:
    """Split a raw SNMP index into normalized numeric parts."""
    parts = [p for p in str(raw_index).split(".") if p.isdigit()]
    if len(parts) >= 4:
        return tuple(parts[-4:])
    if len(parts) >= 2:
        # Packed format: <packed_fsp>.<onu_id>
        return tuple(parts[-2:])
    return None


def _reading_sort_key(index: str) -> tuple[int, ...]:
    """Stable numeric sort key for ONU indexes."""
    parts = [int(p) for p in str(index).split(".") if p.isdigit()]
    if not parts:
        return (10**9,)
    return tuple(parts)


def _fsp_hint_from_index(raw_index: str) -> str | None:
    """Return frame/slot/port hint from a composite SNMP index."""
    parsed = _split_onu_index(raw_index)
    if not parsed or len(parsed) != 4:
        return None
    frame, slot, port, _onu = parsed
    return f"{frame}/{slot}/{port}"


def _fsp_hint_from_ont(ont: OntUnit) -> str | None:
    """Derive frame/slot/port hint from ONT board/port fields."""
    board_parts = [p for p in str(getattr(ont, "board", "")).split("/") if p.isdigit()]
    port_parts = [p for p in str(getattr(ont, "port", "")).split("/") if p.isdigit()]

    if len(port_parts) >= 3:
        return f"{port_parts[-3]}/{port_parts[-2]}/{port_parts[-1]}"
    if len(board_parts) >= 2 and len(port_parts) >= 1:
        return f"{board_parts[-2]}/{board_parts[-1]}/{port_parts[-1]}"
    return None


def _build_reading_targets(
    db: Session,
    *,
    olt: OLTDevice,
    readings: list[OntSignalReading],
    assignments: list[OntAssignment],
) -> list[tuple[OntUnit, OntSignalReading]]:
    """Map SNMP readings to ONTs using external_id/FSP hints, then fallback order."""
    assignment_ont_ids = [
        ont_id
        for ont_id in (a.ont_unit_id for a in assignments)
        if ont_id is not None
    ]
    direct_ont_ids = list(
        db.scalars(
            select(OntUnit.id)
            .where(OntUnit.olt_device_id == olt.id)
            .where(OntUnit.is_active.is_(True))
        ).all()
    )

    ordered_ids: list = []
    seen_ids: set = set()
    for ont_id in assignment_ont_ids + direct_ont_ids:
        if ont_id in seen_ids:
            continue
        seen_ids.add(ont_id)
        ordered_ids.append(ont_id)

    if not ordered_ids:
        return []

    id_to_ont: dict = {
        ont.id: ont
        for ont in db.scalars(select(OntUnit).where(OntUnit.id.in_(ordered_ids))).all()
    }
    ordered_onts = [id_to_ont[ont_id] for ont_id in ordered_ids if ont_id in id_to_ont]
    if not ordered_onts:
        return []

    by_external_id: dict[str, OntUnit] = {}
    by_fsp_hint: dict[str, list[OntUnit]] = {}
    for ont in ordered_onts:
        external_id = str(getattr(ont, "external_id", "") or "").strip().lower()
        if external_id:
            by_external_id[external_id] = ont
        fsp_hint = _fsp_hint_from_ont(ont)
        if fsp_hint:
            by_fsp_hint.setdefault(fsp_hint, []).append(ont)

    used_ont_ids: set = set()
    fallback_queue = list(ordered_onts)
    targets: list[tuple[OntUnit, OntSignalReading]] = []
    vendor_lower = str(getattr(olt, "vendor", "") or "").lower()

    for reading in sorted(readings, key=lambda r: _reading_sort_key(r.onu_index)):
        matched: OntUnit | None = None

        # 1) Exact external_id match (preferred, deterministic)
        external_candidates = []
        if "huawei" in vendor_lower:
            external_candidates.append(f"huawei:{reading.onu_index}")
        if "zte" in vendor_lower:
            external_candidates.append(f"zte:{reading.onu_index}")
        if "nokia" in vendor_lower:
            external_candidates.append(f"nokia:{reading.onu_index}")
        external_candidates.append(reading.onu_index)

        for candidate in external_candidates:
            ont = by_external_id.get(candidate.lower())
            if ont and ont.id not in used_ont_ids:
                matched = ont
                break

        # 2) FSP hint match (frame/slot/port)
        if matched is None:
            fsp_hint = _fsp_hint_from_index(reading.onu_index)
            if fsp_hint:
                for ont in by_fsp_hint.get(fsp_hint, []):
                    if ont.id not in used_ont_ids:
                        matched = ont
                        break

        # 3) Fallback to ordered ONT queue to preserve legacy behavior
        if matched is None:
            while fallback_queue:
                candidate = fallback_queue.pop(0)
                if candidate.id not in used_ont_ids:
                    matched = candidate
                    break

        if matched is None:
            continue

        used_ont_ids.add(matched.id)
        targets.append((matched, reading))

    return targets


def _get_olt_snmp_config(db: Session, olt: OLTDevice) -> dict[str, str | int | None]:
    """Build SNMP config dict for an OLT device.

    OLT devices don't store SNMP auth directly; resolve a linked
    NetworkDevice by mgmt_ip/hostname/name and read SNMP fields there.
    """
    host = olt.mgmt_ip or olt.hostname
    vendor = (olt.vendor or "").lower()
    community: str | None = None

    linked: NetworkDevice | None = None
    if olt.mgmt_ip:
        linked = db.scalars(
            select(NetworkDevice).where(NetworkDevice.mgmt_ip == olt.mgmt_ip).limit(1)
        ).first()
    if linked is None and olt.hostname:
        linked = db.scalars(
            select(NetworkDevice).where(NetworkDevice.hostname == olt.hostname).limit(1)
        ).first()
    if linked is None and olt.name:
        linked = db.scalars(
            select(NetworkDevice).where(NetworkDevice.name == olt.name).limit(1)
        ).first()

    if linked and linked.snmp_enabled:
        if (linked.snmp_version or "v2c").lower() != "v2c":
            logger.warning(
                "Skipping OLT %s SNMP poll: unsupported SNMP version '%s' (only v2c supported)",
                olt.name,
                linked.snmp_version,
            )
        else:
            raw_community = (linked.snmp_community or "").strip() or None
            community = decrypt_credential(raw_community) if raw_community else None

    return {
        "host": host,
        "vendor": vendor,
        "community": community,
    }


def _resolve_oid_set(vendor: str) -> dict[str, str]:
    """Return the OID set for a given vendor, or generic fallback."""
    vendor_lower = vendor.lower().strip()
    for key, oids in _VENDOR_OID_OIDS.items():
        if key in vendor_lower:
            return oids
    return GENERIC_OIDS


def _get_signal_scale(vendor: str) -> float:
    """Return the signal value scale factor for a vendor."""
    vendor_lower = vendor.lower().strip()
    for key, scale in _VENDOR_SIGNAL_SCALE.items():
        if key in vendor_lower:
            return scale
    return 0.01


def _run_olt_snmpwalk(host: str, oid: str, community: str, timeout: int = 30) -> list[str]:
    """Run snmpwalk against an OLT and return output lines."""
    import subprocess

    args = [
        "snmpwalk",
        "-t",
        "10",
        "-r",
        "2",
        "-m",
        "",
        "-v2c",
        "-c",
        community,
        host,
        oid,
    ]
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "No Such Object" in stderr or "No Such Instance" in stderr:
            return []
        if stderr:
            logger.warning(
                "SNMP walk failed for %s OID %s: %s", host, oid, stderr[:200]
            )
            return []
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _normalize_numeric_oid(oid: str) -> str:
    """Normalize an OID token to numeric-dot notation (strip 'iso.')."""
    raw = oid.strip()
    if raw.startswith("iso."):
        return raw.replace("iso.", "1.", 1)
    if raw.startswith("."):
        return raw[1:]
    return raw


def _extract_index_from_oid(oid_part: str, base_oid: str | None = None) -> str | None:
    """Extract SNMP table index from full OID, using base OID when provided."""
    normalized = _normalize_numeric_oid(oid_part)
    if base_oid:
        base = _normalize_numeric_oid(base_oid)
        if normalized.startswith(base + "."):
            return normalized[len(base) + 1 :]
    parts = normalized.rsplit(".", 1)
    if len(parts) < 2:
        return None
    return parts[-1]


def _parse_snmp_table(lines: list[str], *, base_oid: str | None = None) -> dict[str, str]:
    """Parse SNMP walk output into {index: value} dict."""
    parsed: dict[str, str] = {}
    for line in lines:
        if " = " not in line:
            continue
        oid_part, value_part = line.split(" = ", 1)
        index = _extract_index_from_oid(oid_part, base_oid=base_oid)
        if not index:
            continue
        # Extract value after type prefix
        value = value_part.split(": ", 1)[-1].strip().strip('"')
        if value.lower().startswith("no such"):
            continue
        parsed[index] = value
    return parsed


def _parse_snmp_table_composite(
    lines: list[str], *, base_oid: str | None = None
) -> dict[str, str]:
    """Parse SNMP walk output preserving composite indexes (e.g., shelf.slot.port.onu)."""
    parsed: dict[str, str] = {}
    for line in lines:
        if " = " not in line:
            continue
        oid_part, value_part = line.split(" = ", 1)
        index = _extract_index_from_oid(oid_part, base_oid=base_oid)
        if not index:
            continue
        value = value_part.split(": ", 1)[-1].strip().strip('"')
        if value.lower().startswith("no such"):
            continue
        parsed[index] = value
    return parsed


def _parse_signal_value(
    raw: str,
    scale: float = 0.01,
    *,
    vendor: str = "",
    metric: str = "olt_rx",
    stats: dict[str, int] | None = None,
) -> float | None:
    """Parse an SNMP signal value string to dBm float.

    Supports vendor/metric-specific decoding and explicit sentinel handling.
    """
    match = re.search(r"(-?\d+)", raw)
    if not match:
        if stats is not None:
            stats["missing"] = stats.get("missing", 0) + 1
        return None
    try:
        raw_int = int(match.group(1))
    except ValueError:
        if stats is not None:
            stats["parse_error"] = stats.get("parse_error", 0) + 1
        return None

    if raw_int in _SIGNAL_SENTINELS:
        if stats is not None:
            stats["sentinel"] = stats.get("sentinel", 0) + 1
        return None

    vendor_lower = vendor.lower().strip()

    # Huawei ONU Rx commonly reports offset integer values, e.g. 7113 -> -28.87 dBm.
    if "huawei" in vendor_lower and metric == "onu_rx" and raw_int > 1000:
        dbm = (raw_int - 10000) / 100.0
    else:
        # Vendors report in 0.01 dBm units typically.
        dbm = raw_int * scale

    # Sanity check — optical signals are typically between 0 and -45 dBm
    if dbm < -50.0 or dbm > 10.0:
        # Might be in different units, try as-is
        if -50.0 <= raw_int <= 10.0:
            if stats is not None:
                stats["parsed"] = stats.get("parsed", 0) + 1
            return float(raw_int)
        if stats is not None:
            stats["out_of_range"] = stats.get("out_of_range", 0) + 1
        return None
    if stats is not None:
        stats["parsed"] = stats.get("parsed", 0) + 1
    return dbm


def _parse_distance(raw: str) -> int | None:
    """Parse distance value from SNMP (meters)."""
    match = re.search(r"(\d+)", raw)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    # Treat tiny values as unknown sentinel responses from some OLTs.
    if value <= 1:
        return None
    return value


def _parse_online_status(raw: str) -> bool | None:
    """Parse ONU online status from SNMP value."""
    lowered = raw.lower().strip()
    match = re.search(r"(\d+)", lowered)
    if match:
        code = int(match.group(1))
        # Known vendor conventions: 1=online, 2/3/4/5=offline states.
        if code == 1:
            return True
        if code in {2, 3, 4, 5}:
            return False
        return None
    if "online" in lowered or "up" in lowered:
        return True
    if "offline" in lowered or "down" in lowered:
        return False
    return None


def _derive_offline_reason(raw: str) -> str | None:
    """Derive offline reason from SNMP status value."""
    lowered = raw.lower().strip()
    match = re.search(r"(\d+)", lowered)
    if match:
        code = int(match.group(1))
        if code == 1:
            return None  # Online — no offline reason
        if code == 3:
            return "power_fail"
        if code == 4:
            return "los"
        if code == 5:
            return "dying_gasp"
        return "unknown"
    if "power" in lowered:
        return "power_fail"
    if "los" in lowered or "signal" in lowered:
        return "los"
    if "dying" in lowered:
        return "dying_gasp"
    return "unknown" if "offline" in lowered or "down" in lowered else None


# ---------------------------------------------------------------------------
# Main polling orchestration
# ---------------------------------------------------------------------------


def poll_olt_ont_signals(
    db: Session,
    olt: OLTDevice,
    *,
    community: str | None = None,
) -> dict[str, int]:
    """Poll all ONTs on an OLT for optical signal levels via SNMP.

    Updates OntUnit records with signal data in bulk.

    Args:
        db: Database session.
        olt: OLT device to poll.
        community: SNMP community string.

    Returns:
        Stats dict: {polled, updated, errors, skipped}.
    """
    host = olt.mgmt_ip or olt.hostname
    if not host:
        logger.warning("OLT %s has no management IP or hostname, skipping", olt.name)
        return {"polled": 0, "updated": 0, "errors": 0, "skipped": 1}
    if not community:
        logger.warning("OLT %s has no SNMP community configured, skipping", olt.name)
        return {"polled": 0, "updated": 0, "errors": 0, "skipped": 1}

    vendor = (olt.vendor or "").lower()
    oids = _resolve_oid_set(vendor)
    scale = _get_signal_scale(vendor)

    # Huawei ONT indexes are composite (e.g., 4194320384.0), preserve full indexes.
    parse_table = _parse_snmp_table_composite if "huawei" in vendor else _parse_snmp_table

    # Walk signal tables from OLT
    olt_rx_raw = parse_table(
        _run_olt_snmpwalk(host, oids["olt_rx"], community),
        base_oid=oids["olt_rx"],
    )
    onu_rx_raw = parse_table(
        _run_olt_snmpwalk(host, oids["onu_rx"], community),
        base_oid=oids["onu_rx"],
    )
    distance_raw = (
        parse_table(
            _run_olt_snmpwalk(host, oids.get("distance", ""), community),
            base_oid=oids.get("distance", ""),
        )
        if oids.get("distance")
        else {}
    )
    status_raw = (
        parse_table(
            _run_olt_snmpwalk(host, oids.get("status", ""), community),
            base_oid=oids.get("status", ""),
        )
        if oids.get("status")
        else {}
    )

    if not olt_rx_raw and not onu_rx_raw and not status_raw:
        logger.info("No SNMP signal data returned for OLT %s (%s)", olt.name, host)
        return {"polled": 0, "updated": 0, "errors": 0, "skipped": 0}

    # Build readings keyed by ONU index
    all_indexes = (
        set(olt_rx_raw.keys()) | set(onu_rx_raw.keys()) | set(status_raw.keys())
    )
    readings: list[OntSignalReading] = []
    parse_stats: dict[str, int] = {
        "parsed": 0,
        "sentinel": 0,
        "out_of_range": 0,
        "missing": 0,
        "parse_error": 0,
    }
    for idx in all_indexes:
        readings.append(
            OntSignalReading(
                onu_index=idx,
                olt_rx_dbm=_parse_signal_value(
                    olt_rx_raw.get(idx, ""),
                    scale,
                    vendor=vendor,
                    metric="olt_rx",
                    stats=parse_stats,
                ),
                onu_rx_dbm=_parse_signal_value(
                    onu_rx_raw.get(idx, ""),
                    scale,
                    vendor=vendor,
                    metric="onu_rx",
                    stats=parse_stats,
                ),
                distance_m=_parse_distance(distance_raw.get(idx, "")),
                is_online=_parse_online_status(status_raw.get(idx, "")),
            )
        )

    polled = len(readings)
    logger.info(
        "Polled %d ONT signal readings from OLT %s (parsed=%d sentinel=%d out_of_range=%d)",
        polled,
        olt.name,
        parse_stats.get("parsed", 0),
        parse_stats.get("sentinel", 0),
        parse_stats.get("out_of_range", 0),
    )

    # Map readings to OntUnit records via assignments
    # Get all active assignments for this OLT's PON ports
    stmt = (
        select(OntAssignment)
        .join(PonPort, OntAssignment.pon_port_id == PonPort.id)
        .where(
            PonPort.olt_id == olt.id,
            OntAssignment.active.is_(True),
        )
    )
    assignments = list(db.scalars(stmt).all())

    now = datetime.now(UTC)
    updated = 0
    errors = 0

    targets = _build_reading_targets(
        db,
        olt=olt,
        readings=readings,
        assignments=assignments,
    )
    for ont, reading in targets:
        try:
            update_values: dict = {}
            if reading.olt_rx_dbm is not None:
                update_values["olt_rx_signal_dbm"] = reading.olt_rx_dbm
            if reading.onu_rx_dbm is not None:
                update_values["onu_rx_signal_dbm"] = reading.onu_rx_dbm
            if reading.distance_m is not None:
                update_values["distance_meters"] = reading.distance_m

            if reading.is_online is not None:
                if reading.is_online:
                    update_values["online_status"] = OnuOnlineStatus.online
                    update_values["last_seen_at"] = now
                    update_values["offline_reason"] = None
                else:
                    update_values["online_status"] = OnuOnlineStatus.offline
                    status_val = status_raw.get(reading.onu_index, "")
                    reason = _derive_offline_reason(status_val)
                    if reason is None:
                        update_values["offline_reason"] = None
                    else:
                        try:
                            update_values["offline_reason"] = OnuOfflineReason(reason)
                        except ValueError:
                            update_values["offline_reason"] = OnuOfflineReason.unknown

            # Mark telemetry freshness only when at least one field was observed.
            if update_values:
                update_values["signal_updated_at"] = now
            else:
                continue

            db.execute(
                update(OntUnit)
                .where(OntUnit.id == ont.id)
                .values(**update_values)
            )
            updated += 1
        except Exception as e:
            logger.error("Error updating ONT %s: %s", ont.id, e)
            errors += 1

    skipped = max(0, polled - len(targets))
    return {"polled": polled, "updated": updated, "errors": errors, "skipped": skipped}


# ---------------------------------------------------------------------------
# OLT hardware health OIDs (per-vendor)
# ---------------------------------------------------------------------------

_OLT_HEALTH_OIDS: dict[str, dict[str, str]] = {
    "huawei": {
        "cpu": ".1.3.6.1.4.1.2011.6.3.4.1.2.0",       # hwAvgDuty1min
        "temperature": ".1.3.6.1.4.1.2011.6.3.4.1.3.0", # hwEntityTemperature
        "memory": ".1.3.6.1.4.1.2011.6.3.4.1.8.0",      # hwMemoryUtilization
        "uptime": ".1.3.6.1.2.1.1.3.0",                  # sysUpTime (standard)
    },
    "zte": {
        "cpu": ".1.3.6.1.4.1.3902.1082.500.1.2.1.0",
        "temperature": ".1.3.6.1.4.1.3902.1082.500.1.2.2.0",
        "memory": ".1.3.6.1.4.1.3902.1082.500.1.2.3.0",
        "uptime": ".1.3.6.1.2.1.1.3.0",
    },
    "nokia": {
        "cpu": ".1.3.6.1.4.1.637.61.1.9.37.0",
        "temperature": ".1.3.6.1.4.1.637.61.1.9.50.0",
        "memory": ".1.3.6.1.4.1.637.61.1.9.38.0",
        "uptime": ".1.3.6.1.2.1.1.3.0",
    },
}

# Standard MIB-II fallback
_GENERIC_HEALTH_OIDS: dict[str, str] = {
    "uptime": ".1.3.6.1.2.1.1.3.0",  # sysUpTime
}


@dataclass(frozen=True)
class OltHealthReading:
    """Health snapshot for a single OLT."""

    cpu_percent: float | None = None
    temperature_c: float | None = None
    memory_percent: float | None = None
    uptime_seconds: int | None = None


def _snmpget_value(host: str, oid: str, community: str) -> str | None:
    """Perform a single SNMP GET and return the value string, or None."""
    import subprocess

    args = ["snmpget", "-t", "5", "-r", "1", "-m", "", "-v2c", "-c", community, host, oid]
    result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=15)
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    if " = " not in output:
        return None
    value_part = output.split(" = ", 1)[1]
    val = value_part.split(": ", 1)[-1].strip().strip('"')
    if val.lower().startswith("no such"):
        return None
    return val


def _parse_numeric(raw: str | None) -> float | None:
    """Extract a numeric value from an SNMP string."""
    if not raw:
        return None
    match = re.search(r"(-?\d+\.?\d*)", raw)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _parse_uptime_ticks(raw: str | None) -> int | None:
    """Parse sysUpTime (hundredths of a second) to seconds."""
    if not raw:
        return None
    # sysUpTime is in TimeTicks (1/100 sec), but may include human text
    # e.g. "Timeticks: (1234567) 14 days, 6:56:07.67"
    match = re.search(r"\((\d+)\)", raw)
    if match:
        ticks = int(match.group(1))
        return ticks // 100
    # Fallback: plain numeric
    match = re.search(r"(\d+)", raw)
    if match:
        return int(match.group(1)) // 100
    return None


def poll_olt_health(
    olt: OLTDevice,
    *,
    community: str | None = None,
) -> OltHealthReading:
    """Poll OLT hardware health metrics via SNMP.

    Args:
        olt: OLT device to poll.
        community: SNMP community string.

    Returns:
        OltHealthReading with available metrics.
    """
    host = olt.mgmt_ip or olt.hostname
    if not host:
        return OltHealthReading()
    if not community:
        return OltHealthReading()

    vendor = (olt.vendor or "").lower().strip()
    oids: dict[str, str] = {}
    for key, vendor_oids in _OLT_HEALTH_OIDS.items():
        if key in vendor:
            oids = vendor_oids
            break
    if not oids:
        oids = _GENERIC_HEALTH_OIDS

    cpu_raw = _snmpget_value(host, oids["cpu"], community) if "cpu" in oids else None
    temp_raw = (
        _snmpget_value(host, oids["temperature"], community)
        if "temperature" in oids
        else None
    )
    mem_raw = (
        _snmpget_value(host, oids["memory"], community) if "memory" in oids else None
    )
    uptime_raw = (
        _snmpget_value(host, oids["uptime"], community) if "uptime" in oids else None
    )

    cpu = _parse_numeric(cpu_raw)
    temperature = _parse_numeric(temp_raw)
    memory = _parse_numeric(mem_raw)
    uptime = _parse_uptime_ticks(uptime_raw)

    # Clamp percentages to 0-100 range
    if cpu is not None:
        cpu = max(0.0, min(100.0, cpu))
    if memory is not None:
        memory = max(0.0, min(100.0, memory))

    return OltHealthReading(
        cpu_percent=cpu,
        temperature_c=temperature,
        memory_percent=memory,
        uptime_seconds=uptime,
    )


# ---------------------------------------------------------------------------
# Metrics push to VictoriaMetrics
# ---------------------------------------------------------------------------

_VM_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")


def _push_signal_metrics(db: Session) -> int:
    """Push per-ONT signal metrics and aggregate status counts to VictoriaMetrics.

    Reads current signal data from the database and writes Prometheus line
    protocol to VictoriaMetrics' import endpoint (sync HTTP).

    Returns:
        Number of metric lines written.
    """
    # Collect ONTs with recent signal data and their OLT/PON info
    stmt = (
        select(
            OntUnit.serial_number,
            OntUnit.olt_rx_signal_dbm,
            OntUnit.onu_rx_signal_dbm,
            OntUnit.online_status,
            OLTDevice.name.label("olt_name"),
            PonPort.name.label("pon_port_name"),
        )
        .select_from(OntUnit)
        .outerjoin(
            OntAssignment,
            (OntAssignment.ont_unit_id == OntUnit.id) & (OntAssignment.active.is_(True)),
        )
        .outerjoin(PonPort, PonPort.id == OntAssignment.pon_port_id)
        .outerjoin(OLTDevice, OLTDevice.id == func.coalesce(PonPort.olt_id, OntUnit.olt_device_id))
        .where(
            OntUnit.is_active.is_(True),
            OntUnit.signal_updated_at.is_not(None),
        )
    )
    rows = db.execute(stmt).all()

    if not rows:
        return 0

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    lines: list[str] = []

    seen_serials: set[str] = set()
    for row in rows:
        serial = row.serial_number
        if not serial or serial in seen_serials:
            continue
        seen_serials.add(serial)
        olt_name = row.olt_name or "unknown"
        pon_port = row.pon_port_name or "unknown"
        labels = f'ont_serial="{serial}",olt_name="{olt_name}",pon_port="{pon_port}"'

        if row.olt_rx_signal_dbm is not None:
            lines.append(f"ont_olt_rx_dbm{{{labels}}} {row.olt_rx_signal_dbm} {now_ms}")
        if row.onu_rx_signal_dbm is not None:
            lines.append(f"ont_onu_rx_dbm{{{labels}}} {row.onu_rx_signal_dbm} {now_ms}")

    # Aggregate status counts
    status_counts = db.execute(
        select(OntUnit.online_status, func.count())
        .where(OntUnit.is_active.is_(True))
        .group_by(OntUnit.online_status)
    ).all()

    for status_val, count in status_counts:
        status_str = status_val.value if hasattr(status_val, "value") else str(status_val)
        lines.append(f'onu_status_total{{status="{status_str}"}} {count} {now_ms}')

    # Signal quality counts
    warn_thresh, crit_thresh = get_signal_thresholds(db)
    warning_count = db.scalar(
        select(func.count())
        .select_from(OntUnit)
        .where(
            OntUnit.is_active.is_(True),
            OntUnit.olt_rx_signal_dbm.is_not(None),
            OntUnit.olt_rx_signal_dbm < warn_thresh,
            OntUnit.olt_rx_signal_dbm >= crit_thresh,
        )
    ) or 0
    critical_count = db.scalar(
        select(func.count())
        .select_from(OntUnit)
        .where(
            OntUnit.is_active.is_(True),
            OntUnit.olt_rx_signal_dbm.is_not(None),
            OntUnit.olt_rx_signal_dbm < crit_thresh,
        )
    ) or 0
    lines.append(f'onu_signal_low{{severity="warning"}} {warning_count} {now_ms}')
    lines.append(f'onu_signal_low{{severity="critical"}} {critical_count} {now_ms}')

    if not lines:
        return 0

    # Write to VictoriaMetrics
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{_VM_URL}/api/v1/import/prometheus",
                content="\n".join(lines),
                headers={"Content-Type": "text/plain"},
            )
            resp.raise_for_status()
        logger.info("Pushed %d ONT signal metric lines to VictoriaMetrics", len(lines))
    except httpx.HTTPError as e:
        logger.error("Failed to push signal metrics to VictoriaMetrics: %s", e)

    return len(lines)


def _push_olt_health_metrics(health_map: dict[str, OltHealthReading]) -> int:
    """Push OLT health metrics to VictoriaMetrics.

    Args:
        health_map: Dict of OLT name -> OltHealthReading.

    Returns:
        Number of metric lines written.
    """
    if not health_map:
        return 0

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    lines: list[str] = []

    for olt_name, reading in health_map.items():
        labels = f'olt_name="{olt_name}"'
        if reading.cpu_percent is not None:
            lines.append(f"olt_cpu_percent{{{labels}}} {reading.cpu_percent} {now_ms}")
        if reading.temperature_c is not None:
            lines.append(
                f"olt_temperature_celsius{{{labels}}} {reading.temperature_c} {now_ms}"
            )
        if reading.memory_percent is not None:
            lines.append(
                f"olt_memory_percent{{{labels}}} {reading.memory_percent} {now_ms}"
            )
        if reading.uptime_seconds is not None:
            lines.append(
                f"olt_uptime_seconds{{{labels}}} {reading.uptime_seconds} {now_ms}"
            )

    if not lines:
        return 0

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{_VM_URL}/api/v1/import/prometheus",
                content="\n".join(lines),
                headers={"Content-Type": "text/plain"},
            )
            resp.raise_for_status()
        logger.info("Pushed %d OLT health metric lines to VictoriaMetrics", len(lines))
    except httpx.HTTPError as e:
        logger.error("Failed to push OLT health metrics to VictoriaMetrics: %s", e)

    return len(lines)


def poll_all_olts(db: Session) -> dict[str, int]:
    """Poll all active OLT devices for ONT signal levels and OLT health.

    Returns:
        Aggregate stats: {olts_polled, total_polled, total_updated, total_errors}.
    """
    stmt = select(OLTDevice).where(OLTDevice.is_active.is_(True))
    olts = list(db.scalars(stmt).all())

    if not olts:
        logger.info("No active OLTs found for signal polling")
        return {
            "olts_polled": 0,
            "total_polled": 0,
            "total_updated": 0,
            "total_errors": 0,
        }

    totals: dict[str, int] = {
        "olts_polled": 0,
        "total_polled": 0,
        "total_updated": 0,
        "total_errors": 0,
    }

    health_map: dict[str, OltHealthReading] = {}

    for olt in olts:
        snmp_cfg = _get_olt_snmp_config(db, olt)
        community = (
            str(snmp_cfg.get("community")).strip()
            if snmp_cfg.get("community") is not None
            else None
        )
        try:
            result = poll_olt_ont_signals(db, olt, community=community)
            totals["olts_polled"] += 1
            totals["total_polled"] += result["polled"]
            totals["total_updated"] += result["updated"]
            totals["total_errors"] += result["errors"]
        except Exception as e:
            logger.error("Failed to poll OLT %s: %s", olt.name, e)
            totals["total_errors"] += 1

        # Poll OLT hardware health
        try:
            health = poll_olt_health(olt, community=community)
            health_map[olt.name] = health
        except Exception as e:
            logger.error("Failed to poll health for OLT %s: %s", olt.name, e)

    db.commit()

    # Push signal metrics to VictoriaMetrics after DB updates
    try:
        metrics_count = _push_signal_metrics(db)
        totals["metrics_pushed"] = metrics_count
    except Exception as e:
        logger.error("Signal metrics push failed: %s", e)
        totals["metrics_pushed"] = 0

    # Push OLT health metrics
    try:
        health_count = _push_olt_health_metrics(health_map)
        totals["health_metrics_pushed"] = health_count
    except Exception as e:
        logger.error("OLT health metrics push failed: %s", e)
        totals["health_metrics_pushed"] = 0

    logger.info(
        "OLT signal polling complete: %d OLTs, %d ONTs polled, %d updated, %d errors",
        totals["olts_polled"],
        totals["total_polled"],
        totals["total_updated"],
        totals["total_errors"],
    )
    return totals
