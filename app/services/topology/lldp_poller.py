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

import re
import uuid

from sqlalchemy.orm import Session

from app.models.network_monitoring import NetworkDevice, TopologyLinkMedium

SOURCE = "lldp_neighbor"


def _norm(value: str | None) -> str:
    """Lowercase + collapse runs of whitespace/hyphens to a single space."""
    return re.sub(r"[\s\-]+", " ", (value or "").strip().lower())


def _neighbor_identity(nb: dict) -> str:
    return str(nb.get("identity") or "")


def _neighbor_address(nb: dict) -> str | None:
    return nb.get("address4") or nb.get("address") or None


def build_device_index(session: Session) -> tuple[dict[str, NetworkDevice], dict[str, NetworkDevice]]:
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
