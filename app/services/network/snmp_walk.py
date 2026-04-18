"""SNMP command helpers."""

from __future__ import annotations

import subprocess  # nosec
from typing import Any

from app.services.credential_crypto import decrypt_credential


def run_simple_v2c_walk(
    linked: Any, oid: str, *, timeout: int = 45, bulk: bool = False
) -> list[str]:
    """Run SNMP walk with minimal flags for Huawei compatibility."""
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

    cmd = "snmpbulkwalk" if bulk else "snmpwalk"
    result = subprocess.run(  # noqa: S603
        [cmd, "-v2c", "-c", community, host, oid],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "SNMP walk failed").strip()
        raise RuntimeError(f"{oid}: {err}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
