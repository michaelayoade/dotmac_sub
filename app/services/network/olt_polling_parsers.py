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

# Characters that should never appear in SNMP host/OID parameters
_SNMP_DANGEROUS_CHARS = frozenset("\n\r;|&`$(){}[]<>\\\"'")

# Signal value sanity bounds (dBm)
_SIGNAL_MIN_DBM = -50.0
_SIGNAL_MAX_DBM = 10.0


class SNMPValidationError(Exception):
    """Raised when SNMP parameters fail validation."""

    pass


def _validate_snmp_host(host: str) -> str:
    """Validate SNMP host is a safe IP address or hostname.

    Args:
        host: IP address or hostname string.

    Returns:
        Validated host string.

    Raises:
        SNMPValidationError: If host contains dangerous characters or is invalid.
    """
    import ipaddress

    if not host or not isinstance(host, str):
        raise SNMPValidationError("SNMP host is required")

    host = host.strip()

    # Check for dangerous characters that could be used for injection
    if any(c in host for c in _SNMP_DANGEROUS_CHARS):
        raise SNMPValidationError(f"SNMP host contains invalid characters: {host}")

    # Check for spaces (not allowed in hostnames/IPs)
    if " " in host:
        raise SNMPValidationError(f"SNMP host contains spaces: {host}")

    # Try to parse as IP address first (most common case)
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    # Validate as hostname (alphanumeric, dots, hyphens only)
    import re

    if not re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?$", host):
        raise SNMPValidationError(f"SNMP host is not a valid hostname: {host}")

    if len(host) > 253:
        raise SNMPValidationError(f"SNMP hostname too long: {len(host)} chars")

    return host


def _validate_snmp_oid(oid: str) -> str:
    """Validate SNMP OID format.

    Args:
        oid: OID string (e.g., ".1.3.6.1.2.1.1.5.0").

    Returns:
        Validated OID string.

    Raises:
        SNMPValidationError: If OID format is invalid.
    """
    if not oid or not isinstance(oid, str):
        raise SNMPValidationError("SNMP OID is required")

    oid = oid.strip()

    # Check for dangerous characters
    if any(c in oid for c in _SNMP_DANGEROUS_CHARS):
        raise SNMPValidationError(f"SNMP OID contains invalid characters: {oid}")

    # OID should only contain digits, dots, and optionally start with 'iso'
    import re

    if not re.match(r"^(iso)?\.?[0-9]+(\.[0-9]+)*$", oid):
        raise SNMPValidationError(f"SNMP OID format invalid: {oid}")

    return oid


def _validate_snmp_community(community: str) -> str:
    """Validate SNMP community string.

    Args:
        community: Community string.

    Returns:
        Validated community string.

    Raises:
        SNMPValidationError: If community contains dangerous characters.
    """
    if not community or not isinstance(community, str):
        raise SNMPValidationError("SNMP community is required")

    # Check for characters that could break command line parsing
    # Note: community strings can contain many characters, but not shell metacharacters
    dangerous = frozenset("\n\r;|&`$(){}<>\\")
    if any(c in community for c in dangerous):
        raise SNMPValidationError("SNMP community contains invalid characters")

    if len(community) > 256:
        raise SNMPValidationError("SNMP community string too long")

    return community


def _run_olt_snmpwalk(
    host: str, oid: str, community: str, timeout: int = 90, *, max_retries: int = 2
) -> list[str]:
    """Run snmpbulkwalk (with snmpwalk fallback) against an OLT and return output lines.

    Args:
        host: OLT IP address or hostname.
        oid: SNMP OID to walk.
        community: SNMP v2c community string.
        timeout: Total timeout in seconds.
        max_retries: Number of retries on transient failures (P2 fix).

    Returns:
        List of SNMP output lines.

    Raises:
        SNMPValidationError: If parameters fail validation.
    """
    import shutil
    import subprocess  # nosec
    import time

    # P0: Validate all parameters before subprocess execution
    host = _validate_snmp_host(host)
    oid = _validate_snmp_oid(oid)
    community = _validate_snmp_community(community)

    # Prefer snmpbulkwalk for performance on large tables
    use_bulk = shutil.which("snmpbulkwalk") is not None
    cmd = "snmpbulkwalk" if use_bulk else "snmpwalk"
    args = [
        cmd,
        "-t",
        "30",  # 30 second timeout per request (some OLTs are slow)
        "-r",
        "1",   # 1 retry (total 60s max per OID)
        "-m",
        "",
        "-v2c",
        "-c",
        community,
        host,
        oid,
    ]

    # P2: Retry on transient failures with exponential backoff
    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(  # noqa: S603
                args,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )

            if result.returncode == 0:
                return [line.strip() for line in result.stdout.splitlines() if line.strip()]

            stderr = result.stderr.strip()
            if "No Such Object" in stderr or "No Such Instance" in stderr:
                return []

            last_error = stderr[:200] if stderr else f"exit code {result.returncode}"

            # Don't retry on definitive errors (authentication, no response)
            if "Timeout" not in stderr and "No Response" not in stderr:
                break

            if attempt < max_retries:
                sleep_time = 2 ** attempt  # 1s, 2s, 4s...
                logger.debug(
                    "SNMP walk retry %d/%d for %s OID %s after %ds",
                    attempt + 1, max_retries, host, oid, sleep_time
                )
                time.sleep(sleep_time)

        except subprocess.TimeoutExpired:
            last_error = f"timeout after {timeout}s"
            if attempt < max_retries:
                logger.debug(
                    "SNMP walk timeout retry %d/%d for %s OID %s",
                    attempt + 1, max_retries, host, oid
                )
                continue
            break

    if last_error:
        logger.warning("SNMP walk failed for %s OID %s: %s", host, oid, last_error)
    return []


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

    # P1: Sanity bounds check — optical signals are typically between 0 and -45 dBm
    # Reject values outside reasonable range to prevent data corruption from
    # malformed SNMP responses or misconfigured OIDs
    if dbm < _SIGNAL_MIN_DBM or dbm > _SIGNAL_MAX_DBM:
        # Might be in different units, try as-is
        if _SIGNAL_MIN_DBM <= raw_int <= _SIGNAL_MAX_DBM:
            if stats is not None:
                stats["parsed"] = stats.get("parsed", 0) + 1
            return float(raw_int)
        if stats is not None:
            stats["out_of_range"] = stats.get("out_of_range", 0) + 1
        logger.debug(
            "Signal value out of range: raw=%d, computed=%.2f dBm (valid: %.1f to %.1f)",
            raw_int, dbm, _SIGNAL_MIN_DBM, _SIGNAL_MAX_DBM
        )
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


# DDM value sanity bounds
_DDM_TEMP_MIN_C = -40.0
_DDM_TEMP_MAX_C = 100.0
_DDM_VOLTAGE_MIN_V = 0.0
_DDM_VOLTAGE_MAX_V = 5.0
_DDM_BIAS_MIN_MA = 0.0
_DDM_BIAS_MAX_MA = 150.0


def _parse_ddm_value(
    raw: str,
    *,
    scale: float = 1.0,
    min_value: float | None = None,
    max_value: float | None = None,
    metric: str = "",
) -> float | None:
    """Parse a generic DDM numeric value from SNMP (temperature, voltage, bias current).

    Args:
        raw: Raw SNMP value string.
        scale: Multiplier to convert raw integer to real units.
        min_value: Minimum valid value (P1 bounds check).
        max_value: Maximum valid value (P1 bounds check).
        metric: Metric name for logging.

    Returns:
        Parsed float value, or None if unparseable/missing/out of bounds.
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

    value = round(raw_int * scale, 4)

    # P1: Bounds checking for DDM values
    if min_value is not None and value < min_value:
        logger.debug(
            "DDM %s value below minimum: %.4f < %.4f", metric or "value", value, min_value
        )
        return None
    if max_value is not None and value > max_value:
        logger.debug(
            "DDM %s value above maximum: %.4f > %.4f", metric or "value", value, max_value
        )
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
