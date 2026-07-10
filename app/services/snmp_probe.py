"""Direct SNMP probes for monitoring devices (reachability + IF-MIB counters)."""

from __future__ import annotations

import shutil
import subprocess  # nosec
from dataclasses import dataclass

from app.services.credential_crypto import decrypt_credential

_SYS_DESCR_OID = "1.3.6.1.2.1.1.1.0"

# IF-MIB octet counters. 64-bit HC counters need SNMPv2c; v1 falls back to the
# 32-bit legacy columns (wraps faster, but rate() at query time handles resets).
_IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
_IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"
_IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"
_IF_OUT_OCTETS = "1.3.6.1.2.1.2.2.1.16"

# snmpget requests are chunked so a long monitored-interface list never
# overflows a single PDU.
_MAX_OIDS_PER_REQUEST = 24


@dataclass(frozen=True)
class SnmpProbeResult:
    handled: bool
    success: bool
    error: str | None = None


def _snmp_version(value: str | None) -> str | None:
    normalized = str(value or "2c").strip().lower()
    if normalized in {"1", "v1"}:
        return "1"
    if normalized in {"2", "2c", "v2", "v2c"}:
        return "2c"
    return None


def probe_snmp_reachability(
    device,
    *,
    timeout_seconds: int = 2,
) -> SnmpProbeResult:
    """Return whether a device answers a simple SNMP sysDescr request.

    This is intentionally a reachability probe, not a telemetry collector. It
    proves the app/worker can reach UDP/161 with the configured community even
    when Zabbix has not attached SNMP items yet.
    """
    binary = shutil.which("snmpget")
    if not binary:
        return SnmpProbeResult(False, False, "snmpget_not_installed")

    host = str(
        getattr(device, "mgmt_ip", None) or getattr(device, "hostname", "") or ""
    )
    host = host.strip()
    if not host:
        return SnmpProbeResult(False, False, "missing_host")

    port = getattr(device, "snmp_port", None)
    if port:
        host = f"{host}:{port}"

    version = _snmp_version(getattr(device, "snmp_version", None))
    if version is None:
        return SnmpProbeResult(False, False, "unsupported_snmp_version")

    raw_community = getattr(device, "snmp_community", None)
    community = decrypt_credential(raw_community) if raw_community else ""
    if not community:
        return SnmpProbeResult(False, False, "missing_snmp_community")

    try:
        result = subprocess.run(  # noqa: S603
            [
                binary,
                f"-v{version}",
                "-c",
                community,
                "-t",
                str(max(1, int(timeout_seconds))),
                "-r",
                "0",
                host,
                _SYS_DESCR_OID,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=max(2, int(timeout_seconds) + 1),
        )
    except subprocess.TimeoutExpired:
        return SnmpProbeResult(True, False, "timeout")
    except OSError as exc:
        return SnmpProbeResult(True, False, exc.__class__.__name__)

    if result.returncode == 0:
        return SnmpProbeResult(True, True)

    error = (result.stderr or result.stdout or "snmpget_failed").strip()
    return SnmpProbeResult(True, False, error[:240])


@dataclass(frozen=True)
class InterfaceOctets:
    """Raw IF-MIB octet counter readings for one ifIndex."""

    in_octets: int | None
    out_octets: int | None


def _snmp_target(device) -> tuple[list[str], str] | None:
    """Common snmpget argument prefix + host for a device, or None if unusable."""
    binary = shutil.which("snmpget")
    if not binary:
        return None
    host = str(
        getattr(device, "mgmt_ip", None) or getattr(device, "hostname", "") or ""
    ).strip()
    if not host:
        return None
    port = getattr(device, "snmp_port", None)
    if port:
        host = f"{host}:{port}"
    version = _snmp_version(getattr(device, "snmp_version", None))
    if version is None:
        return None
    raw_community = getattr(device, "snmp_community", None)
    community = decrypt_credential(raw_community) if raw_community else ""
    if not community:
        return None
    return [binary, f"-v{version}", "-c", community], host


def fetch_interface_octets(
    device,
    snmp_indexes: list[int],
    *,
    timeout_seconds: int = 3,
) -> dict[int, InterfaceOctets] | None:
    """Read in/out octet counters for the given ifIndexes in one sweep.

    Returns ``{ifIndex: InterfaceOctets}`` (indexes the agent didn't answer for
    are absent) or None when the device can't be queried at all (no snmpget,
    no address/community, unsupported version, timeout).
    """
    if not snmp_indexes:
        return {}
    target = _snmp_target(device)
    if target is None:
        return None
    prefix, host = target

    version = _snmp_version(getattr(device, "snmp_version", None))
    in_base = _IF_IN_OCTETS if version == "1" else _IF_HC_IN_OCTETS
    out_base = _IF_OUT_OCTETS if version == "1" else _IF_HC_OUT_OCTETS

    oid_map: dict[str, tuple[int, str]] = {}
    for idx in snmp_indexes:
        oid_map[f"{in_base}.{idx}"] = (idx, "in")
        oid_map[f"{out_base}.{idx}"] = (idx, "out")

    values: dict[str, int] = {}
    oids = list(oid_map)
    for start in range(0, len(oids), _MAX_OIDS_PER_REQUEST):
        chunk = oids[start : start + _MAX_OIDS_PER_REQUEST]
        try:
            result = subprocess.run(  # noqa: S603
                [
                    *prefix,
                    "-t",
                    str(max(1, int(timeout_seconds))),
                    "-r",
                    "0",
                    "-Oqn",
                    host,
                    *chunk,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=max(2, int(timeout_seconds) + 1),
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        for line in (result.stdout or "").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            oid, raw_value = parts[0].lstrip("."), parts[1].strip()
            try:
                values[oid] = int(raw_value)
            except ValueError:
                continue  # noSuchInstance / non-numeric varbind

    readings: dict[int, InterfaceOctets] = {}
    for oid, (idx, direction) in oid_map.items():
        if oid.lstrip(".") not in values:
            continue
        current = readings.get(idx, InterfaceOctets(None, None))
        value = values[oid.lstrip(".")]
        readings[idx] = InterfaceOctets(
            in_octets=value if direction == "in" else current.in_octets,
            out_octets=value if direction == "out" else current.out_octets,
        )
    return readings
