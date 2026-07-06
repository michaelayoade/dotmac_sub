"""LLDP neighbor poller -> directed NetworkTopologyLink (Phase 2).

Reads each MikroTik router's ``/ip/neighbor`` (LLDP/CDP/MNDP discovery, enabled
fleet-wide via the ``lldp-infra`` setting) and builds the device-level directed
graph that the empty sysmap never provided. Read-only against routers; the
reconcile owns ``source='lldp_neighbor'`` rows (upsert + soft-prune) and never
touches manual/other links.

Fetch mechanism: the neighbor tables are read over the RouterOS **binary API**
(port 8728, the ``routeros_api`` library) — the exact transport the bandwidth
poller (``app/poller/mikrotik_poller.py``) already uses against these routers.
The REST API is *not* enabled on the fleet: every ``/rest/...`` call returns
400 "no such command or directory (rest)", so the previous NAS-centric REST
path saw zero neighbors and the backbone graph never populated. We iterate the
``routers`` table directly (each row carries the same user account for API and
REST) and map each router to its ``network_device_id``.

Match: neighbor ``identity`` (normalized) -> network_device name/hostname, then
``address``/``address4`` -> mgmt_ip. Empty identity or no match -> dropped
(CPE/unknown).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import Counter
from datetime import UTC, datetime

import routeros_api
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import Session

from app.models.network_monitoring import (
    NetworkDevice,
    NetworkTopologyLink,
    TopologyLinkMedium,
)
from app.models.router_management import Router
from app.services.credential_crypto import decrypt_credential

logger = logging.getLogger(__name__)

SOURCE = "lldp_neighbor"

# RouterOS binary API port used fleet-wide for bandwidth polling. REST (443) is
# disabled on every production router, so neighbors are read here instead.
BINARY_API_PORT = 8728

# Discovery-grade per-router socket timeout. This is a read-only hourly poll —
# "skip this router this hour, retry next hour" beats hanging on a silently-
# dropping router. 2/25 routinely time out (heavily-loaded cores); they are
# counted, not fatal.
ROUTER_SOCKET_TIMEOUT = 15.0

# Wall-clock safety net: the Celery task has soft_time_limit=300 — stop
# attempting new devices before that so the run finishes cleanly (upsert +
# prune) instead of timing out mid-fleet.
TIME_BUDGET_SECONDS = 240.0

# routeros_api surfaces the cleartext password in some exception strings; strip
# it before anything reaches the logs.
_PASSWORD_RE = re.compile(r"=password=[^\x00 ]*")


def _sanitize_exc(exc: BaseException) -> str:
    """Strip routeros_api's cleartext =password=... from exception text."""
    message = _PASSWORD_RE.sub("=password=<redacted>", str(exc))
    return message or type(exc).__name__


def _norm(value: str | None) -> str:
    """Lowercase + collapse runs of whitespace/hyphens to a single space."""
    return re.sub(r"[\s\-]+", " ", (value or "").strip().lower())


def _neighbor_identity(nb: dict) -> str:
    return str(nb.get("identity") or "")


def _neighbor_address(nb: dict) -> str | None:
    return nb.get("address4") or nb.get("address") or None


def build_device_index(
    session: Session,
) -> tuple[dict[str, NetworkDevice], dict[str, NetworkDevice]]:
    """Index active nodes by normalized name/hostname and by mgmt_ip."""
    by_name: dict[str, NetworkDevice] = {}
    by_ip: dict[str, NetworkDevice] = {}
    for d in (
        session.query(NetworkDevice).filter(NetworkDevice.is_active.is_(True)).all()
    ):
        for label in (d.name, d.hostname):
            n = _norm(label)
            if n:
                by_name.setdefault(n, d)
        if d.mgmt_ip:
            by_ip.setdefault(d.mgmt_ip, d)
    return by_name, by_ip


def match_in_index(index, nb: dict) -> NetworkDevice | None:
    """Match a neighbor against a prebuilt (by_name, by_ip) index."""
    by_name, by_ip = index
    norm = _norm(_neighbor_identity(nb))
    if norm and norm in by_name:
        return by_name[norm]
    addr = _neighbor_address(nb)
    if addr and addr in by_ip:
        return by_ip[addr]
    return None


def match_neighbor(session: Session, nb: dict) -> NetworkDevice | None:
    """Match a single ``/ip/neighbor`` row to a known network_device, or None.

    Priority: normalized identity -> name/hostname, then address -> mgmt_ip.
    Empty identity with no IP hit (CPE) or no match at all returns None.
    """
    return match_in_index(build_device_index(session), nb)


# --- Edge building -----------------------------------------------------------


def _medium(local_iface: str | None) -> TopologyLinkMedium:
    i = (local_iface or "").lower()
    if i.startswith("sfp"):
        return TopologyLinkMedium.fiber
    if i.startswith("ether"):
        return TopologyLinkMedium.ethernet
    return TopologyLinkMedium.unknown


def _canonical(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Order a device pair deterministically so A->B and B->A collapse to one."""
    return (a, b) if str(a) <= str(b) else (b, a)


def accumulate_edges(
    edges: dict, local: NetworkDevice, neighbors: list[dict], index
) -> dict:
    """Add this node's matched neighbor edges into ``edges`` (keyed by canonical
    device pair). Drops empty-identity/unmatched neighbors (CPE/unknown) and
    self-links; first observation of a pair wins (A<->B + repeats dedup).

    The neighbor ``interface`` string (e.g. ``"sfp-sfpplus3=>AFR Fiber"`` from
    the binary API) is the local port on ``local`` and is stashed on the edge."""
    for nb in neighbors:
        remote = match_in_index(index, nb)
        if remote is None or remote.id == local.id:
            continue
        key = _canonical(local.id, remote.id)
        if key in edges:
            continue
        local_iface = nb.get("interface") or ""
        edges[key] = {
            "source_device_id": key[0],
            "target_device_id": key[1],
            "medium": _medium(local_iface),
            "metadata": {
                "observed_from": str(local.id),
                "local_interface": local_iface,
                "remote_identity": _neighbor_identity(nb),
                "remote_board": (
                    nb.get("board") or nb.get("board-name") or nb.get("platform")
                ),
            },
        }
    return edges


# --- Connection + poll -------------------------------------------------------


def _read_neighbors_via_binary_api(router: Router, pool_factory=None) -> list[dict]:
    """Read a router's ``/ip/neighbor`` over the RouterOS binary API (8728).

    Mirrors the bandwidth poller's proven connection path: construct a
    ``RouterOsApiPool`` on 8728 with ``plaintext_login`` and the router's
    decrypted API credentials (RouterOS uses the same user accounts for API and
    REST), then read ``/ip/neighbor``.

    CONNECTION HYGIENE: the pool opens a session on the router, so it is ALWAYS
    disconnected in ``finally`` — including when ``get_api()`` or the read fails
    after the pool was constructed — otherwise the session lingers for days and
    the fleet accrues an 8728 leak (see PR #819 / the bandwidth poller's
    ``_release_pool``). ``pool_factory`` is injectable for tests.
    """
    pool_factory = pool_factory or routeros_api.RouterOsApiPool
    username = decrypt_credential(router.rest_api_username) or router.rest_api_username
    password = decrypt_credential(router.rest_api_password) or router.rest_api_password

    pool = pool_factory(
        router.management_ip,
        username=username,
        password=password,
        port=BINARY_API_PORT,
        plaintext_login=True,
    )
    try:
        # Bound blocking socket I/O so a half-open/firewalled router can't hang
        # this poll (best-effort: tolerate older library builds lacking it).
        try:
            pool.set_timeout(ROUTER_SOCKET_TIMEOUT)
        except Exception:  # noqa: BLE001 - timeout tuning is best-effort
            pass
        api = pool.get_api()
        rows = list(api.get_resource("/ip/neighbor").get())
        return [dict(row) for row in rows]
    finally:
        # The pool (and thus the router-side session) exists the moment
        # pool_factory returned; release it no matter how we leave.
        try:
            pool.disconnect()
        except Exception as exc:  # noqa: BLE001 - never mask the real error
            logger.warning(
                "lldp_poll_pool_disconnect_failed router=%s: %s",
                router.name,
                _sanitize_exc(exc),
            )


def poll_all(
    session: Session,
    read_neighbors=None,
    now: datetime | None = None,
    time_budget_seconds: float = TIME_BUDGET_SECONDS,
) -> dict:
    """Poll every active router's neighbors, upsert lldp_neighbor edges, soft-prune.

    Iterates active ``routers`` rows and reads each one's ``/ip/neighbor`` over
    the binary API (``via_binary_api``). A router maps to its
    ``network_device_id``; a router without one is counted in
    ``skipped_no_device`` (it cannot anchor an edge). A router that is
    unreachable or errors mid-read is counted in ``routers_failed`` and skipped
    — it never aborts the run or prunes others' edges (2/25 routinely time out).
    Once ``time_budget_seconds`` of wall clock is spent, remaining routers are
    counted in ``skipped_time_budget`` and the run still reconciles what it saw.
    A ``SoftTimeLimitExceeded`` raised mid-poll propagates so the task's
    graceful timeout handler fires instead of running into the hard kill.

    Idempotent: edges are keyed by canonical device pair (NULL interfaces), so a
    re-run only bumps ``last_seen_at``.
    """
    read_neighbors = read_neighbors or _read_neighbors_via_binary_api
    now = now or datetime.now(UTC)
    started = time.monotonic()
    budget_logged = False
    index = build_device_index(session)
    stats: Counter = Counter(
        {
            "routers_polled": 0,
            "routers_failed": 0,
            "via_binary_api": 0,
            "skipped_no_device": 0,
            "skipped_time_budget": 0,
            "neighbors_seen": 0,
            "created": 0,
            "updated": 0,
            "pruned": 0,
            "edges": 0,
        }
    )

    routers = session.query(Router).filter(Router.is_active.is_(True)).all()

    edges: dict = {}
    # NetworkDevice ids of routers we SUCCESSFULLY read this run. Only these
    # devices could have re-observed (or stopped observing) their neighbors, so
    # only their edges are eligible for pruning below. Routers that failed
    # (routers_failed) or were skipped (skipped_time_budget / no-device) never
    # enter this set, so edges whose only observer was unreachable this cycle
    # are left untouched instead of flapping active/inactive every run.
    polled_device_ids: set = set()
    for router in routers:
        local = (
            session.get(NetworkDevice, router.network_device_id)
            if router.network_device_id
            else None
        )
        if local is None:
            stats["skipped_no_device"] += 1
            logger.info(
                "lldp_poll_skipped_no_device router=%s (no network_device_id)",
                router.name,
            )
            continue
        elapsed = time.monotonic() - started
        if elapsed > time_budget_seconds:
            if not budget_logged:
                logger.warning(
                    "lldp_poll_time_budget_exhausted after %.0fs; "
                    "skipping remaining routers this run",
                    elapsed,
                )
                budget_logged = True
            stats["skipped_time_budget"] += 1
            continue
        try:
            neighbors = read_neighbors(router)
        except SoftTimeLimitExceeded:
            # Celery's soft timeout must reach the task's graceful handler —
            # counting it as routers_failed would keep looping until the hard
            # time_limit SIGKILLs the worker.
            raise
        except Exception as exc:  # one unreachable router must not abort the run
            stats["routers_failed"] += 1
            logger.warning(
                "lldp_poll_router_failed router=%s: %s",
                router.name,
                _sanitize_exc(exc),
            )
            continue
        stats["routers_polled"] += 1
        stats["via_binary_api"] += 1
        stats["neighbors_seen"] += len(neighbors)
        polled_device_ids.add(local.id)
        accumulate_edges(edges, local, neighbors, index)

    # Upsert by canonical pair, scoped to our source (query-before-insert: the
    # 4-tuple unique constraint treats NULL interfaces as distinct, so we keep
    # one-row-per-pair in code).
    seen_pairs: set = set()
    for key, e in edges.items():
        seen_pairs.add(key)
        link = (
            session.query(NetworkTopologyLink)
            .filter(
                NetworkTopologyLink.source == SOURCE,
                NetworkTopologyLink.source_device_id == e["source_device_id"],
                NetworkTopologyLink.target_device_id == e["target_device_id"],
                NetworkTopologyLink.source_interface_id.is_(None),
                NetworkTopologyLink.target_interface_id.is_(None),
            )
            .first()
        )
        if link is None:
            session.add(
                NetworkTopologyLink(
                    source_device_id=e["source_device_id"],
                    target_device_id=e["target_device_id"],
                    source=SOURCE,
                    medium=e["medium"],
                    metadata_=e["metadata"],
                    is_active=True,
                    discovered_at=now,
                    last_seen_at=now,
                )
            )
            stats["created"] += 1
        else:
            link.medium = e["medium"]
            link.metadata_ = e["metadata"]
            link.is_active = True
            link.last_seen_at = now
            stats["updated"] += 1
    session.flush()

    # Soft-prune our rows not seen this run — but ONLY when a router that could
    # have re-observed the edge was actually polled this run and didn't report
    # it. An edge whose only observing router failed or was skipped this cycle
    # is left active (it'll be re-verified once that router is reachable again),
    # otherwise the 2/25 routers that routinely time out would flap their edges
    # active/inactive on every run and churn the topology graph.
    pruned = 0
    for link in (
        session.query(NetworkTopologyLink)
        .filter(
            NetworkTopologyLink.source == SOURCE,
            NetworkTopologyLink.is_active.is_(True),
        )
        .all()
    ):
        if _canonical(link.source_device_id, link.target_device_id) in seen_pairs:
            continue
        observed_by_polled_router = (
            link.source_device_id in polled_device_ids
            or link.target_device_id in polled_device_ids
        )
        if observed_by_polled_router:
            link.is_active = False
            pruned += 1
    session.flush()

    stats["edges"] = len(edges)
    stats["pruned"] = pruned
    return dict(stats)
