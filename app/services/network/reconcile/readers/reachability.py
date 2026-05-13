"""ICMP reachability check for ONT mgmt IPs.

The reconciler runs from the same host as the GenieACS NBI, which has the
WireGuard route to all per-OLT mgmt subnets. A simple ICMP ping from this
host is the cheapest "is the ONT mgmt plane reachable right now" signal
the planner can use to decide whether ACS-side pushes are worth attempting.

The actual ping runs via ``subprocess`` against the system ``ping`` binary
(works without elevated privileges on Linux containers with the
``net.ipv4.ping_group_range`` sysctl set to include the container user —
the default on most modern setups). Tests substitute a stub via
``ping_function`` parameter.

Failures are silent — a False return means "ONT is not pingable right
now", which the planner treats as one input to the reachability signal,
not the only one.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable

logger = logging.getLogger(__name__)


PingFunction = Callable[[str, int, float], bool]
"""Signature: ``(ip, count, timeout_sec) -> bool``. Default impl shells out
to ``ping``; tests pass a stub."""


def _ping_subprocess(ip: str, count: int, timeout_sec: float) -> bool:
    """Run the system ``ping`` binary against ``ip``.

    Returns True iff at least one ICMP echo reply was received within the
    per-packet timeout. Non-existent ``ping`` binary, permission errors,
    or any non-zero exit code → False (caller treats this as "not pingable").
    """
    ping_bin = shutil.which("ping")
    if ping_bin is None:
        logger.debug("ping_binary_not_found")
        return False
    try:
        result = subprocess.run(
            [
                ping_bin,
                "-c",
                str(count),
                "-W",
                str(int(timeout_sec)),
                "-q",
                ip,
            ],
            capture_output=True,
            timeout=count * timeout_sec + 2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    except OSError as exc:
        logger.debug("ping_subprocess_failed", extra={"ip": ip, "error": str(exc)})
        return False
    return result.returncode == 0


def is_pingable(
    ip: str | None,
    *,
    count: int = 2,
    timeout_sec: float = 2.0,
    ping_function: PingFunction | None = None,
) -> bool:
    """Return True iff the given IP responds to ICMP echo.

    Args:
        ip: Mgmt IP to ping. ``None`` returns False (no IP = not pingable).
        count: ICMP echo requests to send. 2 is the operationally-quick value.
        timeout_sec: Per-packet timeout in seconds. Total cap is roughly
            ``count * timeout_sec + 2``.
        ping_function: Override for the actual ICMP send. Tests substitute
            this; production uses ``_ping_subprocess``.
    """
    if not ip:
        return False
    fn = ping_function or _ping_subprocess
    try:
        return fn(ip, count, timeout_sec)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("ping_function_raised", extra={"ip": ip, "error": str(exc)})
        return False


__all__ = ("PingFunction", "is_pingable")
