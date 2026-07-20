from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.network_monitoring import (
    DeviceInterface,
    NetworkDevice,
    NetworkTopologyLink,
    PopSite,
)
from app.services.network.forwarding_topology import (
    execute_forwarding_topology_decision,
    preview_forwarding_topology_decision,
    propose_forwarding_topology_decision,
    review_forwarding_topology_decision,
)


def _site_for(db: Session, device: NetworkDevice) -> PopSite:
    if device.pop_site_id is not None:
        site = db.get(PopSite, device.pop_site_id)
        assert site is not None
        return site
    site = PopSite(name=f"Test site {device.name} {uuid.uuid4().hex[:8]}")
    db.add(site)
    db.flush()
    device.pop_site_id = site.id
    db.flush()
    return site


def declare_forwarding_edge(
    db: Session,
    downstream: NetworkDevice,
    upstream: NetworkDevice,
    *,
    downstream_role: str = "access",
    upstream_role: str = "core",
) -> NetworkTopologyLink:
    """Create an exact LLDP observation and independently reviewed declaration."""

    if downstream.pop_site_id is None and upstream.pop_site_id is not None:
        downstream.pop_site_id = upstream.pop_site_id
    elif upstream.pop_site_id is None and downstream.pop_site_id is not None:
        upstream.pop_site_id = downstream.pop_site_id
    elif downstream.pop_site_id is None and upstream.pop_site_id is None:
        shared_site = PopSite(name=f"Test forwarding site {uuid.uuid4().hex[:8]}")
        db.add(shared_site)
        db.flush()
        downstream.pop_site_id = shared_site.id
        upstream.pop_site_id = shared_site.id
    db.flush()
    downstream_site = _site_for(db, downstream)
    upstream_site = _site_for(db, upstream)
    suffix = uuid.uuid4().hex[:8]
    downstream_interface = DeviceInterface(
        device_id=downstream.id,
        name=f"to-{upstream.name}-{suffix}",
    )
    upstream_interface = DeviceInterface(
        device_id=upstream.id,
        name=f"to-{downstream.name}-{suffix}",
    )
    db.add_all([downstream_interface, upstream_interface])
    db.flush()
    link = NetworkTopologyLink(
        source_device_id=downstream.id,
        source_interface_id=downstream_interface.id,
        target_device_id=upstream.id,
        target_interface_id=upstream_interface.id,
        source="lldp_neighbor",
        is_active=True,
    )
    db.add(link)
    db.flush()

    path_key = f"test:{downstream.id}:{upstream.id}:{suffix}"
    declaration = {
        "configuration_intent_ref": f"test-intent:{path_key}",
        "configuration_owner": "network.control_plane_intent",
        "downstream_device_id": str(downstream.id),
        "downstream_interface_id": str(downstream_interface.id),
        "downstream_pop_site_id": str(downstream_site.id),
        "downstream_role": downstream_role,
        "path_key": path_key,
        "path_kind": "internal",
        "preference": 100,
        "upstream_device_id": str(upstream.id),
        "upstream_interface_id": str(upstream_interface.id),
        "upstream_pop_site_id": str(upstream_site.id),
        "upstream_role": upstream_role,
        "vrf_name": "main",
    }
    preview = preview_forwarding_topology_decision(
        db,
        action="declare",
        declaration=declaration,
        path_key=path_key,
        reason="topology test fixture",
        proposed_by="test:proposer",
    )
    decision = propose_forwarding_topology_decision(
        db,
        action="declare",
        declaration=declaration,
        path_key=path_key,
        reason="topology test fixture",
        proposed_by="test:proposer",
        expected_decision_sha256=preview.decision_sha256,
        commit=False,
    )
    review_forwarding_topology_decision(
        db,
        decision.id,
        action="approve",
        reviewed_by="test:reviewer",
        review_notes="independent topology fixture review",
        expected_decision_sha256=decision.decision_sha256,
        commit=False,
    )
    execute_forwarding_topology_decision(
        db,
        decision.id,
        executed_by="test:executor",
        expected_decision_sha256=decision.decision_sha256,
        commit=False,
    )
    return link


__all__ = ["declare_forwarding_edge"]
