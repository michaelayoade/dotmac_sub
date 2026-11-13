"""Reachability classification over Sub's authoritative forwarding graph.

Monitoring polls sites *through* their upstream router, so when one router dies
everything behind it also reads ``down`` — simultaneity is lost visibility,
not multiple failures (the SPDC pattern: one router failure misread as a
site-wide multi-device outage). This module separates the two: every down
device is classified

- ``down``: no down ancestor on its path to core — it IS a root cause; or
- ``unreachable_upstream``: some ancestor on its path to core is down — the
  root cause is the TOPMOST down ancestor (the one nearest core).

Raw LLDP cannot establish ancestry. Only reviewed forwarding declarations with
current exact observation agreement participate in root-cause projection.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.network_monitoring import NetworkDevice
from app.services.topology.affected import forwarding_graph_projection
from app.services.topology.live_status import DOWN

CLASS_DOWN: Literal["down"] = "down"
CLASS_UNREACHABLE_UPSTREAM: Literal["unreachable_upstream"] = "unreachable_upstream"


@dataclass(frozen=True)
class Reachability:
    """Classification of one down device."""

    classification: Literal["down", "unreachable_upstream"]
    root_cause_device_id: UUID


@dataclass(frozen=True)
class ReachabilityRow:
    device: NetworkDevice
    classification: Literal["down", "unreachable_upstream"]
    root_cause: NetworkDevice | None


@dataclass(frozen=True)
class ReachabilityPageQuery:
    page: int = 1
    per_page: int = 20


@dataclass(frozen=True)
class ReachabilityPage:
    rows: tuple[ReachabilityRow, ...]
    page: int
    per_page: int
    total: int
    total_pages: int


def core_parent_map(
    session: Session,
    *,
    adjacency: dict | None = None,
    root_ids: list | set | frozenset | None = None,
) -> dict:
    """Map every reachable node to its next hop toward a declared root."""

    if adjacency is None or root_ids is None:
        graph = forwarding_graph_projection(session)
        if adjacency is None:
            adjacency = graph.adjacency
        if root_ids is None:
            root_ids = graph.root_device_ids
    parent: dict = dict.fromkeys(root_ids)
    queue: deque = deque(root_ids)
    while queue:
        nid = queue.popleft()
        for nb in adjacency.get(nid, ()):
            if nb not in parent:
                parent[nb] = nid
                queue.append(nb)
    return parent


def classify_down_devices(
    session: Session,
    *,
    adjacency: dict | None = None,
    root_ids: list | set | frozenset | None = None,
) -> dict[UUID, Reachability]:
    """Classify every active down device as ``down`` or ``unreachable_upstream``.

    Returns ``{device_id: Reachability}`` — one entry per device whose cached
    ``live_status`` is ``down``. A device with no path to a declared root
    cannot have provable down ancestry, so it degrades to
    ``down`` (its own root cause) rather than being silently swallowed —
    mirroring how ``downstream_nodes`` degrades to the node itself.
    """
    rows = (
        session.query(NetworkDevice.id, NetworkDevice.live_status)
        .filter(NetworkDevice.is_active.is_(True))
        .all()
    )
    down_ids = {r[0] for r in rows if r[1] == DOWN}
    if not down_ids:
        return {}
    parent = core_parent_map(
        session,
        adjacency=adjacency,
        root_ids=root_ids,
    )

    result: dict[UUID, Reachability] = {}
    for device_id in down_ids:
        if device_id not in parent:
            result[device_id] = Reachability(CLASS_DOWN, device_id)
            continue
        # Walk toward core; the LAST down node seen is the topmost down
        # ancestor (nearest core) — the root cause for everything below it.
        topmost_down = None
        cur = parent[device_id]
        while cur is not None:
            if cur in down_ids:
                topmost_down = cur
            cur = parent[cur]
        if topmost_down is None:
            result[device_id] = Reachability(CLASS_DOWN, device_id)
        else:
            result[device_id] = Reachability(CLASS_UNREACHABLE_UPSTREAM, topmost_down)
    return result


def reachability_overview(session: Session) -> tuple[ReachabilityRow, ...]:
    """Operator-facing rows for the outage console: every down device with its
    classification and root cause, root causes first. Read-only."""
    classified = classify_down_devices(session)
    if not classified:
        return ()
    devices = {
        d.id: d
        for d in session.query(NetworkDevice)
        .filter(NetworkDevice.id.in_(classified))
        .all()
    }
    rows: list[ReachabilityRow] = []
    for device_id, info in classified.items():
        device = devices.get(device_id)
        if device is None:
            continue
        root = devices.get(info.root_cause_device_id)
        rows.append(
            ReachabilityRow(
                device=device,
                classification=info.classification,
                root_cause=root if info.root_cause_device_id != device_id else None,
            )
        )
    rows.sort(key=lambda r: (r.classification != CLASS_DOWN, str(r.device.name or "")))
    return tuple(rows)


def get_reachability_page(
    session: Session, *, query: ReachabilityPageQuery
) -> ReachabilityPage:
    """Return one bounded page from the authoritative root-cause projection."""
    if query.page < 1 or not 1 <= query.per_page <= 100:
        raise ValueError("Invalid reachability page")
    rows = reachability_overview(session)
    total_pages = max(1, (len(rows) + query.per_page - 1) // query.per_page)
    page = min(query.page, total_pages)
    start = (page - 1) * query.per_page
    return ReachabilityPage(
        rows=rows[start : start + query.per_page],
        page=page,
        per_page=query.per_page,
        total=len(rows),
        total_pages=total_pages,
    )
