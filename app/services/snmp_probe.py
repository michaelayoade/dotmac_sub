"""Direct SNMP reachability probes for monitoring devices."""

from __future__ import annotations

import shutil
import subprocess  # nosec
from dataclasses import dataclass

from app.services.credential_crypto import decrypt_credential

_SYS_DESCR_OID = "1.3.6.1.2.1.1.1.0"


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

    host = str(getattr(device, "mgmt_ip", None) or getattr(device, "hostname", "") or "")
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
