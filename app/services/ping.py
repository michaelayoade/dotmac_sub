"""Shared ping helpers used by service modules."""

from __future__ import annotations

import ipaddress
import logging
import re
import subprocess

logger = logging.getLogger(__name__)

_MAX_HOST_LENGTH = 253


def _validate_host(host: str) -> None:
    """Validate host is a reasonable IP address or hostname."""
    if not host or len(host) > _MAX_HOST_LENGTH:
        raise ValueError(f"Invalid host: {host!r}")
    # Accept valid IP addresses directly
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    # Basic hostname validation: alphanumeric, hyphens, dots
    if not all(c.isalnum() or c in "-." for c in host):
        raise ValueError(f"Invalid hostname characters: {host!r}")


def is_ipv6_host(host: str) -> bool:
    """Return True when host parses as an IPv6 address."""
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def build_ping_command(host: str) -> list[str]:
    """Build a ping command for IPv4/IPv6 hosts."""
    _validate_host(host)
    command = ["ping", "-c", "1", "-W", "2", host]
    if is_ipv6_host(host):
        command.insert(1, "-6")
    return command


def parse_latency_ms(output: str) -> float | None:
    """Extract ping latency in milliseconds from ping command output."""
    match = re.search(r"time[=<]\s*([0-9.]+)\s*ms", output or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def run_ping(host: str, *, timeout_seconds: int = 4) -> tuple[bool, float | None]:
    """Execute ping and return (success, latency_ms)."""
    try:
        result = subprocess.run(
            build_ping_command(host),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, None
    except (ValueError, OSError) as exc:
        logger.warning("Ping failed for host %s: %s", host, exc)
        return False, None

    if result.returncode != 0:
        return False, None

    output = f"{result.stdout or ''} {result.stderr or ''}"
    return True, parse_latency_ms(output)
