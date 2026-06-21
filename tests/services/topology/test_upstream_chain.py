"""Upstream-chain BFS over the LLDP graph (directed-chain UI, DC.1)."""

from __future__ import annotations

from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
    NetworkTopologyLink,
)
from app.services.topology.customer_path import resolve_upstream_chain


def _dev(db, name, role=DeviceRole.edge):
    d = NetworkDevice(name=name, role=role, is_active=True)
    db.add(d)
    db.flush()
    return d


def _link(db, a, b):
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
    _link(db_session, access, agg)
    _link(db_session, agg, core)

    chain = resolve_upstream_chain(db_session, access)
    assert [d.name for d in chain] == ["Agg", "Core"]  # excludes access; ends at core


def test_cycle_is_safe_and_shortest(db_session):
    access = _dev(db_session, "Access")
    a = _dev(db_session, "A")
    b = _dev(db_session, "B")
    core = _dev(db_session, "Core", role=DeviceRole.core)
    # ring: access-a-b-access, plus a-core
    _link(db_session, access, a)
    _link(db_session, a, b)
    _link(db_session, b, access)
    _link(db_session, a, core)

    chain = resolve_upstream_chain(db_session, access)
    assert [d.name for d in chain] == ["A", "Core"]  # shortest, no infinite loop


def test_no_core_reachable_is_empty(db_session):
    access = _dev(db_session, "Access")
    agg = _dev(db_session, "Agg", role=DeviceRole.aggregation)
    _link(db_session, access, agg)
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
