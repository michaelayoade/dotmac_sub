"""SNMP command helpers."""

from __future__ import annotations

import logging
import shutil
import subprocess  # nosec
from typing import Any

from app.services.credential_crypto import decrypt_credential

logger = logging.getLogger(__name__)

# Default SNMP settings - can be overridden per-OLT via model attributes
DEFAULT_SNMP_TIMEOUT = 45
DEFAULT_BULK_MAX_REPETITIONS = 50


def run_simple_v2c_walk(
    linked: Any,
    oid: str,
    *,
    timeout: int | None = None,
    bulk: bool | None = None,
    max_repetitions: int | None = None,
) -> list[str]:
    """Run SNMP walk with minimal flags for Huawei compatibility.

    Args:
        linked: Object with SNMP credentials (mgmt_ip, hostname, snmp_community, etc.)
        oid: OID to walk
        timeout: Command timeout in seconds (default: 45, or per-OLT setting)
        bulk: Use snmpbulkwalk if True (default: True, or per-OLT setting)
        max_repetitions: GetBulk max-repetitions for efficiency (default: 50)

    Returns:
        List of SNMP response lines
    """
    host = linked.mgmt_ip or linked.hostname
    if not host:
        raise RuntimeError("Missing SNMP host")
    if linked.snmp_port:
        host = f"{host}:{linked.snmp_port}"
    if (linked.snmp_version or "v2c").lower() not in {"v2c", "2c"}:
        raise RuntimeError("Only SNMP v2c is supported for ONU telemetry sync")
    community = (
        decrypt_credential(linked.snmp_community) if linked.snmp_community else ""
    )
    if not community:
        raise RuntimeError("SNMP community is not configured")

    # Resolve settings: explicit param > per-OLT attribute > default
    use_timeout = timeout
    if use_timeout is None:
        use_timeout = getattr(linked, "snmp_timeout_seconds", None) or DEFAULT_SNMP_TIMEOUT

    use_bulk = bulk
    if use_bulk is None:
        use_bulk = getattr(linked, "snmp_bulk_enabled", True)  # Default True for speed

    use_max_reps = max_repetitions
    if use_max_reps is None:
        use_max_reps = getattr(linked, "snmp_bulk_max_repetitions", None) or DEFAULT_BULK_MAX_REPETITIONS

    # Select command and build arguments
    if use_bulk and shutil.which("snmpbulkwalk"):
        cmd = "snmpbulkwalk"
        # -Cr<N> sets max-repetitions for GetBulk PDU efficiency
        args = [cmd, "-v2c", f"-Cr{use_max_reps}", "-c", community, host, oid]
    else:
        cmd = "snmpwalk"
        args = [cmd, "-v2c", "-c", community, host, oid]

    result = subprocess.run(  # noqa: S603
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=use_timeout,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "SNMP walk failed").strip()
        raise RuntimeError(f"{oid}: {err}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
