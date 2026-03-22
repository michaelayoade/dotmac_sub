"""Lightweight SNMP GET wrapper using the net-snmp CLI (snmpget).

Mirrors the subprocess pattern used by snmp_discovery.py for consistency.
No Python SNMP library required — uses the system ``snmpget`` binary.
"""

from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger(__name__)

_NUMERIC_RE = re.compile(r"[-+]?\d+\.?\d*")


def snmp_get(
    host: str,
    community: str,
    oid: str,
    *,
    timeout: int = 8,
    retries: int = 1,
    version: str = "2c",
) -> float | None:
    """Perform a single SNMP GET and return the numeric value.

    Args:
        host: Target device IP or hostname.
        community: SNMP community string.
        oid: OID to query (dotted notation, e.g. ``1.3.6.1.2.1.31.1.1.1.6.3``).
        timeout: Seconds before the request times out.
        retries: Number of retries on failure.
        version: SNMP version (``2c`` or ``1``).

    Returns:
        Numeric value as float, or None if the GET failed or returned
        a non-numeric value.
    """
    args = [
        "snmpget",
        "-v", version,
        "-c", community,
        "-Oqv",          # quiet, value-only output
        "-t", str(timeout),
        "-r", str(retries),
        host,
        oid,
    ]

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        logger.debug("snmpget timed out: %s %s", host, oid)
        return None

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr:
            logger.debug("snmpget error for %s %s: %s", host, oid, stderr)
        return None

    raw = result.stdout.strip()
    if not raw or "No Such" in raw or "Timeout" in raw:
        return None

    match = _NUMERIC_RE.search(raw)
    if not match:
        logger.debug("snmpget non-numeric for %s %s: %s", host, oid, raw)
        return None

    return float(match.group())
