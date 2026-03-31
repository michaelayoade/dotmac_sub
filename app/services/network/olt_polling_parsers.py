"""SNMP response parsing helpers and data classes for OLT polling."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.models.network import OntUnit
from app.services.network.olt_polling_oids import _SIGNAL_SENTINELS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OntSignalReading:
    """Signal reading for a single ONT from SNMP poll."""

    onu_index: str
    olt_rx_dbm: float | None
    onu_rx_dbm: float | None
    onu_tx_dbm: float | None
    distance_m: int | None
    is_online: bool | None
    temperature_c: float | None = None
    voltage_v: float | None = None
    bias_current_ma: float | None = None
    offline_reason_raw: str | None = None
    serial_number_raw: str | None = None


@dataclass(frozen=True)
class OltHealthReading:
    """Health snapshot for a single OLT."""

    cpu_percent: float | None = None
    temperature_c: float | None = None
    memory_percent: float | None = None
    uptime_seconds: int | None = None


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------


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


def _decode_huawei_packed_fsp(packed_value: int) -> str | None:
    """Decode Huawei packed ONU indexes into frame/slot/port when possible."""
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


def _fsp_hint_from_index(raw_index: str) -> str | None:
    """Return frame/slot/port hint from a composite SNMP index."""
    parsed = _split_onu_index(raw_index)
    if not parsed:
        return None
    if len(parsed) == 4:
        frame, slot, port, _onu = parsed
        return f"{frame}/{slot}/{port}"
    if len(parsed) == 2 and parsed[0].isdigit():
        return _decode_huawei_packed_fsp(int(parsed[0]))
    return None


def _fsp_hint_from_ont(ont: OntUnit) -> str | None:
    """Derive frame/slot/port hint from ONT board/port fields."""
    board_parts = [p for p in str(getattr(ont, "board", "")).split("/") if p.isdigit()]
    port_parts = [p for p in str(getattr(ont, "port", "")).split("/") if p.isdigit()]

    if len(port_parts) >= 3:
        return f"{port_parts[-3]}/{port_parts[-2]}/{port_parts[-1]}"
    if len(board_parts) >= 2 and len(port_parts) >= 1:
        return f"{board_parts[-2]}/{board_parts[-1]}/{port_parts[-1]}"
    return None


# ---------------------------------------------------------------------------
# SNMP subprocess helpers
# ---------------------------------------------------------------------------


def _run_olt_snmpwalk(
    host: str, oid: str, community: str, timeout: int = 90
) -> list[str]:
    """Run snmpbulkwalk (with snmpwalk fallback) against an OLT and return output lines."""
    import shutil
    import subprocess  # nosec

    # Prefer snmpbulkwalk for performance on large tables
    use_bulk = shutil.which("snmpbulkwalk") is not None
    cmd = "snmpbulkwalk" if use_bulk else "snmpwalk"
    args = [
        cmd,
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
    result = subprocess.run(  # noqa: S603
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


# ---------------------------------------------------------------------------
# OID / table parsing
# ---------------------------------------------------------------------------


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


def _parse_snmp_table(
    lines: list[str], *, base_oid: str | None = None
) -> dict[str, str]:
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


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------


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


def _parse_ddm_value(raw: str, *, scale: float = 1.0) -> float | None:
    """Parse a generic DDM numeric value from SNMP (temperature, voltage, bias current).

    Args:
        raw: Raw SNMP value string.
        scale: Multiplier to convert raw integer to real units.

    Returns:
        Parsed float value, or None if unparseable/missing.
    """
    if not raw:
        return None
    lowered = raw.lower().strip()
    if lowered.startswith("no such") or lowered == "":
        return None
    match = re.search(r"(-?\d+)", raw)
    if not match:
        return None
    try:
        raw_int = int(match.group(1))
    except ValueError:
        return None
    if raw_int in _SIGNAL_SENTINELS:
        return None
    return round(raw_int * scale, 4)


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
# SNMP GET helpers
# ---------------------------------------------------------------------------


def _snmpget_value(host: str, oid: str, community: str) -> str | None:
    """Perform a single SNMP GET and return the value string, or None."""
    import subprocess  # nosec

    args = [
        "snmpget",
        "-t",
        "5",
        "-r",
        "1",
        "-m",
        "",
        "-v2c",
        "-c",
        community,
        host,
        oid,
    ]
    result = subprocess.run(  # noqa: S603
        args, capture_output=True, text=True, check=False, timeout=15
    )
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


# ---------------------------------------------------------------------------
# sysDescr auto-detection for firmware/software version
# ---------------------------------------------------------------------------


def _parse_sysdescr(raw: str, vendor: str) -> tuple[str | None, str | None]:
    """Parse sysDescr to extract (firmware_version, software_version).

    Uses vendor-specific regex patterns to identify version strings from the
    SNMP sysDescr.0 response.  Returns (firmware, software) where firmware
    typically represents hardware/platform version and software is the running
    OS version.
    """
    vendor_lower = (vendor or "").lower()

    firmware_version: str | None = None
    software_version: str | None = None

    if "huawei" in vendor_lower:
        # Huawei: "Huawei Versatile Routing Platform Software VRP (R) software,
        #          Version 8.210 (MA5800 V300R021C10SPC100)"
        sw_match = re.search(r"Version\s+(V\d+R\d+C\d+\w*)", raw)
        if sw_match:
            software_version = sw_match.group(1)
        hw_match = re.search(r"(MA\d+\S+)", raw)
        if hw_match:
            firmware_version = hw_match.group(1)
    elif "zte" in vendor_lower:
        # ZTE: "ZTE ZXA10 ... Version: V4.1.0P3T2"
        match = re.search(r"Version[:\s]+(V[\d.]+\w*)", raw)
        if match:
            software_version = match.group(1)
    elif "nokia" in vendor_lower:
        # Nokia/ALU: "TiMOS-B-22.10.R3 ..."
        match = re.search(r"TiMOS-([\w.-]+)", raw)
        if match:
            software_version = match.group(1)
    else:
        # Generic fallback: first version-like pattern
        match = re.search(r"(\d+\.\d+[\.\d]*)", raw)
        if match:
            software_version = match.group(1)

    return firmware_version, software_version
