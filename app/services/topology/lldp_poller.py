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
import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.catalog import NasDevice
from app.models.network_monitoring import (
    NetworkDevice,
    NetworkTopologyLink,
    TopologyLinkMedium,
)

logger = logging.getLogger(__name__)

SOURCE = "lldp_neighbor"


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


def poll_all(
    session: Session, read_neighbors=None, now: datetime | None = None
) -> dict:
    """Poll every NAS node's neighbors, upsert lldp_neighbor edges, soft-prune.

    Idempotent: edges are keyed by canonical device pair (NULL interfaces), so a
    re-run only bumps ``last_seen_at``. A NAS that's unreachable (e.g. karsana)
    is counted and skipped — it never aborts the run or prunes others' edges.
    """
    read_neighbors = read_neighbors or _read_ip_neighbors
    now = now or datetime.now(UTC)
    index = build_device_index(session)
    stats: Counter = Counter(
        {
            "nas_polled": 0,
            "nas_failed": 0,
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
        try:
            neighbors = read_neighbors(nas)
        except Exception as exc:  # one unreachable NAS must not abort the run
            stats["nas_failed"] += 1
            logger.warning("lldp_poll_nas_failed node=%s: %s", node.name, exc)
            continue
        stats["nas_polled"] += 1
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
