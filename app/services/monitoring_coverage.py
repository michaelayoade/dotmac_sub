"""Monitoring-path coverage — which device subnets are actually reachable.

Devices live on private networks reached only through WireGuard tunnels. When a
tunnel is down or a subnet has no tunnel at all, Zabbix simply can't reach those
devices — so they read "down" when they may be fine. That's a *blind spot*, not
an outage (see DEVICE_OPERATIONAL_STATUS.md / INFRASTRUCTURE_SLA_PERFORMANCE.md).

This materialises the set of currently-reachable management CIDRs (from the
allowed-IPs of *up* WireGuard peers) and caches it. The operational-status
reader and the SLA availability bridge both consult it: an uncovered device
reads ``unmonitored(no_path)`` instead of a false ``down``, and the SLA bridge
records no downtime for it.

Safety: if ``wg`` is unavailable or returns nothing (dev/test, or a transient
failure), coverage is ``loaded=False`` and ``covers()`` returns ``True`` for
everything — we never *penalise* on missing data, only refine when we have it.
"""

from __future__ import annotations

import ipaddress
import logging
import shutil
import subprocess  # nosec
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

_WG_BIN = shutil.which("wg") or "/usr/bin/wg"
# A peer whose last handshake is within this window is carrying traffic — the
# monitoring warmer constantly pings through live tunnels, so a fresh handshake
# means the path is actually up. Older => treat as no current path.
_HANDSHAKE_FRESH_SECONDS = 900

CACHE_KEY = "monitoring:reachable_cidrs"
_CACHE_TTL_SECONDS = 1800


class MonitoringCoverage:
    """The set of reachable management CIDRs. ``loaded=False`` means we have no
    coverage data and must not penalise (covers() -> True for all)."""

    def __init__(self, cidrs: list[str] | None, *, loaded: bool):
        self.loaded = loaded
        self._networks = []
        for c in cidrs or []:
            try:
                self._networks.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                continue

    def covers(self, ip: str | None) -> bool:
        """True if ``ip`` is within a reachable CIDR. Unloaded coverage or an
        unparseable/absent ip -> True (never penalise on missing data)."""
        if not self.loaded:
            return True
        if not ip:
            return True
        try:
            addr = ipaddress.ip_address(str(ip).split("/")[0])
        except ValueError:
            return True
        return any(addr in net for net in self._networks)

    @property
    def cidr_count(self) -> int:
        return len(self._networks)


def compute_reachable_cidrs(now: datetime | None = None) -> list[str]:
    """Parse ``wg show all dump`` and return allowed-IPs of up (recently
    handshaked) peers. Returns [] when wg is unavailable."""
    now = now or datetime.now(UTC)
    try:
        result = subprocess.run(  # noqa: S603
            [_WG_BIN, "show", "all", "dump"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

    cidrs: set[str] = set()
    for line in result.stdout.strip().split("\n"):
        parts = line.split("\t")
        # `all dump` peer lines: iface pubkey psk endpoint allowed-ips
        # latest-handshake rx tx keepalive  (9 fields). Interface lines have 5.
        if len(parts) < 9:
            continue
        allowed = parts[4]
        try:
            handshake_ts = int(parts[5])
        except ValueError:
            continue
        if handshake_ts <= 0:
            continue
        age = (now - datetime.fromtimestamp(handshake_ts, tz=UTC)).total_seconds()
        if age > _HANDSHAKE_FRESH_SECONDS:
            continue  # peer not currently up -> its subnets aren't reachable now
        for cidr in allowed.split(","):
            cidr = cidr.strip()
            if cidr and cidr != "(none)":
                cidrs.add(cidr)
    return sorted(cidrs)


def refresh_coverage_cache(now: datetime | None = None) -> dict:
    """Recompute reachable CIDRs and store them in the cache (Celery task body)."""
    cidrs = compute_reachable_cidrs(now)
    try:
        from app.services.app_cache import set_json

        # Only overwrite the cache when we actually got data — a transient wg
        # failure shouldn't blank a good cached set into "no coverage".
        if cidrs:
            set_json(CACHE_KEY, cidrs, _CACHE_TTL_SECONDS)
    except Exception:
        logger.debug("coverage_cache_write_failed", exc_info=True)
    return {"cidrs": len(cidrs)}


def get_coverage() -> MonitoringCoverage:
    """Read the cached reachable-CIDR set. Missing cache -> unloaded (safe)."""
    try:
        from app.services.app_cache import get_json

        raw = get_json(CACHE_KEY)
    except Exception:
        raw = None
    if not raw or not isinstance(raw, list):
        return MonitoringCoverage(None, loaded=False)
    return MonitoringCoverage(list(raw), loaded=True)
