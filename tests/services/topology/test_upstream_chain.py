"""Customer upstream chain over the authoritative forwarding projection."""

from __future__ import annotations

from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
    NetworkTopologyLink,
)
from app.services.topology.customer_path import resolve_upstream_chain
from tests.services.topology.forwarding_test_support import declare_forwarding_edge


def _dev(db, name, role=DeviceRole.edge):
    d = NetworkDevice(name=name, role=role, is_active=True)
    db.add(d)
    db.flush()
    return d


def _edge(db, downstream, upstream, *, downstream_role="access", upstream_role="core"):
    return declare_forwarding_edge(
        db,
        downstream,
        upstream,
        downstream_role=downstream_role,
        upstream_role=upstream_role,
    )


def _observe(db, a, b):
    db.add(
        NetworkTopologyLink(
            source_device_id=a.id,
            target_device_id=b.id,
            source="lldp_neighbor",
            is_active=True,
        )
    )
    db.flush()


def test_linear_chain_to_core(db_session):
    access = _dev(db_session, "Access")
    agg = _dev(db_session, "Agg", role=DeviceRole.aggregation)
    core = _dev(db_session, "Core", role=DeviceRole.core)
    _edge(
        db_session,
        access,
        agg,
        downstream_role="access",
        upstream_role="aggregation",
    )
    _edge(
        db_session,
        agg,
        core,
        downstream_role="aggregation",
        upstream_role="core",
    )

    chain = resolve_upstream_chain(db_session, access)
    assert [d.name for d in chain] == ["Agg", "Core"]  # excludes access; ends at core


def test_undeclared_lldp_cycle_cannot_change_reviewed_path(db_session):
    access = _dev(db_session, "Access")
    a = _dev(db_session, "A")
    b = _dev(db_session, "B")
    core = _dev(db_session, "Core", role=DeviceRole.core)
    _edge(
        db_session,
        access,
        a,
        downstream_role="access",
        upstream_role="aggregation",
    )
    _edge(
        db_session,
        a,
        core,
        downstream_role="aggregation",
        upstream_role="core",
    )
    # Raw observations form a ring, but cannot create official path.
    _observe(db_session, a, b)
    _observe(db_session, b, access)

    chain = resolve_upstream_chain(db_session, access)
    assert [d.name for d in chain] == ["A", "Core"]  # shortest, no infinite loop


def test_no_core_reachable_is_empty(db_session):
    access = _dev(db_session, "Access")
    agg = _dev(db_session, "Agg", role=DeviceRole.aggregation)
    _edge(
        db_session,
        access,
        agg,
        downstream_role="access",
        upstream_role="aggregation",
    )
    assert resolve_upstream_chain(db_session, access) == []


def test_isolated_node_is_empty(db_session):
    access = _dev(db_session, "Access")
    assert resolve_upstream_chain(db_session, access) == []


def test_only_lldp_edges_are_walked(db_session):
    access = _dev(db_session, "Access")
    core = _dev(db_session, "Core", role=DeviceRole.core)
    # a non-LLDP / inactive edge must be ignored
    db_session.add(
        NetworkTopologyLink(
            source_device_id=access.id,
            target_device_id=core.id,
            source="manual",
            is_active=True,
        )
    )
    db_session.flush()
    assert resolve_upstream_chain(db_session, access) == []
