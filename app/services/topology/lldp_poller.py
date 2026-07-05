"""LLDP neighbor poller -> directed NetworkTopologyLink (Phase 2).

Reads each MikroTik NAS's ``/ip/neighbor`` (LLDP/CDP/MNDP discovery, enabled
fleet-wide via the ``lldp-infra`` setting) and builds the device-level directed
graph that the empty sysmap never provided. Read-only against routers; the
reconcile owns ``source='lldp_neighbor'`` rows (upsert + soft-prune) and never
touches manual/other links.

Match: neighbor ``identity`` (normalized) -> network_device name/hostname, then
``address4`` -> mgmt_ip. Empty identity or no match -> dropped (CPE/unknown).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import Counter
from datetime import UTC, datetime

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice
from app.models.network_monitoring import (
    NetworkDevice,
    NetworkTopologyLink,
    TopologyLinkMedium,
)

logger = logging.getLogger(__name__)

SOURCE = "lldp_neighbor"

# Discovery-grade REST tunables for the router-credentials fallback. This is a
# read-only hourly poll — "skip this hour, retry next hour" beats spending the
# snapshot-grade retry budget (~90-120s worst case) on each dead router.
ROUTER_CONNECT_TIMEOUT = 5.0
ROUTER_READ_TIMEOUT = 15.0
ROUTER_MAX_RETRIES = 1

# Wall-clock safety net: the Celery task has soft_time_limit=300 — stop
# attempting new devices before that so the run finishes cleanly (upsert +
# prune) instead of timing out mid-fleet.
TIME_BUDGET_SECONDS = 240.0


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

    Priority: normalized identity -> name/hostname, then address4 -> mgmt_ip.
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
    self-links; first observation of a pair wins (A<->B + repeats dedup)."""
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
                "remote_board": nb.get("board") or nb.get("platform"),
            },
        }
    return edges


# --- Connection + poll -------------------------------------------------------


def _read_ip_neighbors(nas: NasDevice) -> list[dict]:
    """Read a MikroTik NAS's ``/ip/neighbor`` over the REST API (read-only)."""
    from app.services.nas import _mikrotik as mt

    base_url, auth, headers, verify_tls = mt._mikrotik_rest_auth(nas)
    data = mt._mikrotik_rest_get(
        base_url=base_url,
        path="/rest/ip/neighbor",
        auth=auth,
        headers=headers,
        verify_tls=verify_tls,
    )
    return data if isinstance(data, list) else []


def _fallback_router(session: Session, nas: NasDevice):
    """Active router-management row linked to this NAS, or None.

    Most NAS rows carry no ``api_url``/REST credentials, but their ``routers``
    row (config-snapshot feature) has ``rest_api_username``/``rest_api_password``
    plus management_ip/port/ssl — reuse those to poll neighbors.
    """
    from app.models.router_management import Router

    return (
        session.query(Router)
        .filter(Router.nas_device_id == nas.id, Router.is_active.is_(True))
        .first()
    )


def _read_ip_neighbors_via_router(router) -> list[dict]:
    """Read ``/ip/neighbor`` using router-management REST credentials.

    Reuses :class:`RouterConnectionService` — the exact connection layer the
    config-snapshot feature uses (credential decryption, management_ip +
    rest_api_port + use_ssl base URL, verify_tls) — but with discovery-grade
    tunables: one attempt, short timeouts. A dead router costs seconds, not
    the snapshot retry budget.
    """
    from app.services.router_management.connection import RouterConnectionService

    data = RouterConnectionService.execute(
        router,
        "GET",
        "/ip/neighbor",
        connect_timeout=ROUTER_CONNECT_TIMEOUT,
        read_timeout=ROUTER_READ_TIMEOUT,
        max_retries=ROUTER_MAX_RETRIES,
    )
    return data if isinstance(data, list) else []


def poll_all(
    session: Session,
    read_neighbors=None,
    read_router_neighbors=None,
    now: datetime | None = None,
    time_budget_seconds: float = TIME_BUDGET_SECONDS,
) -> dict:
    """Poll every NAS node's neighbors, upsert lldp_neighbor edges, soft-prune.

    Per-NAS credential resolution: a NAS with ``api_url`` is polled directly
    (``via_nas``, the original path). Without one, the linked active ``routers``
    row supplies REST credentials the way config snapshots do (``via_router``).
    Jump-host-only routers are skipped (``skipped_jump_host``) — the hourly
    poller does not open SSH tunnels; a NAS with neither config is counted in
    ``skipped_no_creds`` rather than failing. Once ``time_budget_seconds`` of
    wall clock is spent, remaining devices are counted in
    ``skipped_time_budget`` and the run still reconciles what it saw. A
    ``SoftTimeLimitExceeded`` raised mid-poll propagates so the task's graceful
    timeout handler fires instead of running into the hard kill.

    Idempotent: edges are keyed by canonical device pair (NULL interfaces), so a
    re-run only bumps ``last_seen_at``. A NAS that's unreachable (e.g. karsana)
    is counted and skipped — it never aborts the run or prunes others' edges.
    """
    read_neighbors = read_neighbors or _read_ip_neighbors
    read_router_neighbors = read_router_neighbors or _read_ip_neighbors_via_router
    now = now or datetime.now(UTC)
    started = time.monotonic()
    budget_logged = False
    index = build_device_index(session)
    stats: Counter = Counter(
        {
            "nas_polled": 0,
            "nas_failed": 0,
            "via_nas": 0,
            "via_router": 0,
            "skipped_no_creds": 0,
            "skipped_jump_host": 0,
            "skipped_time_budget": 0,
            "neighbors_seen": 0,
            "created": 0,
            "updated": 0,
            "pruned": 0,
            "edges": 0,
        }
    )

    nas_nodes = (
        session.query(NetworkDevice)
        .filter(
            NetworkDevice.matched_device_type == "nas",
            NetworkDevice.matched_device_id.isnot(None),
            NetworkDevice.is_active.is_(True),
        )
        .all()
    )

    edges: dict = {}
    for node in nas_nodes:
        nas = session.get(NasDevice, node.matched_device_id)
        if nas is None:
            continue
        elapsed = time.monotonic() - started
        if elapsed > time_budget_seconds:
            if not budget_logged:
                logger.warning(
                    "lldp_poll_time_budget_exhausted after %.0fs; "
                    "skipping remaining devices this run",
                    elapsed,
                )
                budget_logged = True
            stats["skipped_time_budget"] += 1
            continue
        try:
            if nas.api_url:
                # Original path: NAS row has its own REST config.
                neighbors = read_neighbors(nas)
                via = "via_nas"
            else:
                router = _fallback_router(session, nas)
                if router is None or not (
                    router.rest_api_username and router.rest_api_password
                ):
                    stats["skipped_no_creds"] += 1
                    logger.info(
                        "lldp_poll_skipped_no_creds node=%s (no api_url, no router creds)",
                        node.name,
                    )
                    continue
                access = getattr(router.access_method, "value", router.access_method)
                if access == "jump_host":
                    stats["skipped_jump_host"] += 1
                    logger.info(
                        "lldp_poll_skipped_jump_host node=%s router=%s",
                        node.name,
                        router.name,
                    )
                    continue
                neighbors = read_router_neighbors(router)
                via = "via_router"
        except SoftTimeLimitExceeded:
            # Celery's soft timeout must reach the task's graceful handler —
            # counting it as nas_failed would keep looping until the hard
            # time_limit SIGKILLs the worker.
            raise
        except Exception as exc:  # one unreachable NAS must not abort the run
            stats["nas_failed"] += 1
            logger.warning("lldp_poll_nas_failed node=%s: %s", node.name, exc)
            continue
        stats["nas_polled"] += 1
        stats[via] += 1
        stats["neighbors_seen"] += len(neighbors)
        accumulate_edges(edges, node, neighbors, index)

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

    # Soft-prune our rows not seen this run.
    pruned = 0
    for link in (
        session.query(NetworkTopologyLink)
        .filter(
            NetworkTopologyLink.source == SOURCE,
            NetworkTopologyLink.is_active.is_(True),
        )
        .all()
    ):
        if _canonical(link.source_device_id, link.target_device_id) not in seen_pairs:
            link.is_active = False
            pruned += 1
    session.flush()

    stats["edges"] = len(edges)
    stats["pruned"] = pruned
    return dict(stats)
