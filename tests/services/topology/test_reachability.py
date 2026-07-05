"""Reachability classification: down vs unreachable_upstream (Phase 5a)."""

from __future__ import annotations

from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
    NetworkTopologyLink,
)
from app.services.topology.reachability import (
    CLASS_DOWN,
    CLASS_UNREACHABLE_UPSTREAM,
    classify_down_devices,
    reachability_overview,
)


def _dev(db, name, *, role=DeviceRole.edge, live_status="up"):
    d = NetworkDevice(name=name, role=role, is_active=True, live_status=live_status)
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


def test_router_down_makes_children_unreachable(db_session):
    """The SPDC pattern: router fails, everything behind it reads down —
    ONE root cause, the rest are symptoms."""
    core = _dev(db_session, "Core", role=DeviceRole.core)
    router = _dev(db_session, "Router", live_status="down")
    ap = _dev(db_session, "AP", live_status="down")
    _link(db_session, core, router)
    _link(db_session, router, ap)

    result = classify_down_devices(db_session)
    assert result[router.id].classification == CLASS_DOWN
    assert result[router.id].root_cause_device_id == router.id
    assert result[ap.id].classification == CLASS_UNREACHABLE_UPSTREAM
    assert result[ap.id].root_cause_device_id == router.id


def test_root_cause_is_topmost_down_ancestor(db_session):
    """Two down hops stacked: everything below attributes to the one nearest
    core, not to its immediate parent."""
    core = _dev(db_session, "Core", role=DeviceRole.core)
    r1 = _dev(db_session, "R1", live_status="down")
    r2 = _dev(db_session, "R2", live_status="down")
    ap = _dev(db_session, "AP", live_status="down")
    _link(db_session, core, r1)
    _link(db_session, r1, r2)
    _link(db_session, r2, ap)

    result = classify_down_devices(db_session)
    assert result[r1.id].classification == CLASS_DOWN
    assert result[r2.id].root_cause_device_id == r1.id
    assert result[ap.id].classification == CLASS_UNREACHABLE_UPSTREAM
    assert result[ap.id].root_cause_device_id == r1.id  # topmost, not r2


def test_down_with_healthy_path_is_its_own_root_cause(db_session):
    core = _dev(db_session, "Core", role=DeviceRole.core)
    router = _dev(db_session, "Router")  # up
    ap = _dev(db_session, "AP", live_status="down")
    _link(db_session, core, router)
    _link(db_session, router, ap)

    result = classify_down_devices(db_session)
    assert result[ap.id].classification == CLASS_DOWN
    assert result[ap.id].root_cause_device_id == ap.id


def test_no_core_path_degrades_to_down(db_session):
    """An island (no LLDP path to core) has no provable ancestry — degrade to
    a root cause rather than silently swallowing it."""
    _dev(db_session, "Core", role=DeviceRole.core)
    orphan = _dev(db_session, "Orphan", live_status="down")

    result = classify_down_devices(db_session)
    assert result[orphan.id].classification == CLASS_DOWN
    assert result[orphan.id].root_cause_device_id == orphan.id


def test_up_devices_are_not_classified(db_session):
    core = _dev(db_session, "Core", role=DeviceRole.core)
    router = _dev(db_session, "Router")
    _link(db_session, core, router)
    assert classify_down_devices(db_session) == {}


def test_overview_lists_root_causes_first(db_session):
    core = _dev(db_session, "Core", role=DeviceRole.core)
    router = _dev(db_session, "Router", live_status="down")
    ap = _dev(db_session, "AP", live_status="down")
    _link(db_session, core, router)
    _link(db_session, router, ap)

    rows = reachability_overview(db_session)
    assert [r["device"].name for r in rows] == ["Router", "AP"]
    assert rows[0]["root_cause"] is None  # its own root cause
    assert rows[1]["root_cause"].id == router.id
