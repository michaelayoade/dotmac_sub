"""poll_all upsert + prune + idempotency + failure isolation (Phase 2, P2.4)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.catalog import NasDevice
from app.models.network_monitoring import NetworkDevice, NetworkTopologyLink
from app.models.router_management import Router, RouterAccessMethod
from app.services.topology.lldp_poller import SOURCE, poll_all

NOW = datetime(2026, 6, 17, 14, 0, tzinfo=UTC)


def _nas_node(db, name, mgmt_ip=None, api_url="https://mikrotik.example"):
    nas = NasDevice(name=name, management_ip=mgmt_ip, api_url=api_url)
    db.add(nas)
    db.flush()
    node = NetworkDevice(
        name=name,
        mgmt_ip=mgmt_ip,
        source="zabbix_reconcile",
        matched_device_type="nas",
        matched_device_id=nas.id,
        is_active=True,
    )
    db.add(node)
    db.flush()
    return node, nas


def _plain(db, name, mgmt_ip=None):
    d = NetworkDevice(name=name, mgmt_ip=mgmt_ip, is_active=True)
    db.add(d)
    db.flush()
    return d


def _active_links(db):
    return (
        db.query(NetworkTopologyLink)
        .filter(
            NetworkTopologyLink.source == SOURCE,
            NetworkTopologyLink.is_active.is_(True),
        )
        .all()
    )


def test_poll_all_upsert_idempotent_and_prune(db_session):
    spdc, nas_spdc = _nas_node(db_session, "SPDC Access")
    gbb, nas_gbb = _nas_node(db_session, "GBB")
    switch = _plain(db_session, "SPDC-Switch", mgmt_ip="10.0.0.77")

    neighbors = {
        str(nas_spdc.id): [
            {"identity": "GBB", "interface": "sfp1"},
            {"identity": "", "interface": "ether5", "address4": "192.168.88.50"},  # CPE
            {
                "identity": "sw",
                "address4": "10.0.0.77",
                "interface": "ether2",
            },  # switch by IP
        ],
        str(nas_gbb.id): [
            {"identity": "SPDC Access", "interface": "sfp1"}
        ],  # sees spdc back
    }
    stub = lambda nas: neighbors.get(str(nas.id), [])  # noqa: E731

    r1 = poll_all(
        db_session, read_neighbors=stub, now=datetime(2026, 6, 17, 14, 0, tzinfo=UTC)
    )
    assert r1["nas_polled"] == 2
    assert r1["created"] == 2  # spdc<->gbb (deduped) + spdc<->switch
    assert len(_active_links(db_session)) == 2

    # --- idempotent: run again, no new rows, only last_seen_at bumps ---
    r2 = poll_all(
        db_session, read_neighbors=stub, now=datetime(2026, 6, 17, 14, 5, tzinfo=UTC)
    )
    assert r2["created"] == 0
    assert r2["updated"] == 2
    assert r2["pruned"] == 0
    assert len(_active_links(db_session)) == 2

    # --- prune: spdc stops seeing the switch -> that edge soft-pruned ---
    neighbors[str(nas_spdc.id)] = [{"identity": "GBB", "interface": "sfp1"}]
    r3 = poll_all(
        db_session, read_neighbors=stub, now=datetime(2026, 6, 17, 14, 10, tzinfo=UTC)
    )
    assert r3["pruned"] == 1
    active = _active_links(db_session)
    assert len(active) == 1  # only spdc<->gbb remains
    sw_link = (
        db_session.query(NetworkTopologyLink)
        .filter(
            NetworkTopologyLink.source == SOURCE,
            NetworkTopologyLink.is_active.is_(False),
        )
        .all()
    )
    assert len(sw_link) == 1


def test_unreachable_nas_isolated(db_session):
    ok, nas_ok = _nas_node(db_session, "OK Access")
    bad, nas_bad = _nas_node(db_session, "Karsana Access")
    _plain(db_session, "GBB")

    def stub(nas):
        if nas.id == nas_bad.id:
            raise OSError("unreachable")
        return [{"identity": "GBB", "interface": "sfp1"}]

    r = poll_all(
        db_session, read_neighbors=stub, now=datetime(2026, 6, 17, 14, 0, tzinfo=UTC)
    )
    assert r["nas_failed"] == 1
    assert r["nas_polled"] == 1  # the reachable one still processed
    assert r["created"] == 1


# --- Router-credentials fallback (NAS rows without api_url) -------------------


def _router_for(db, nas, name, **kwargs):
    kwargs.setdefault("hostname", name)
    kwargs.setdefault("management_ip", "10.20.0.1")
    kwargs.setdefault("rest_api_username", "snap-api")
    kwargs.setdefault("rest_api_password", "enc:secret")
    router = Router(name=name, nas_device_id=nas.id, **kwargs)
    db.add(router)
    db.flush()
    return router


def _raise_nas_read(nas):
    raise AssertionError("NAS REST path must not be used without api_url")


def test_nas_with_api_url_uses_nas_path_unchanged(db_session):
    spdc, nas_spdc = _nas_node(db_session, "SPDC Access")  # api_url set by default
    _plain(db_session, "GBB")
    # Even with a linked router, the NAS path wins when api_url is configured.
    _router_for(db_session, nas_spdc, "spdc-rtr")

    def router_read(router):
        raise AssertionError("router fallback must not be used when api_url is set")

    r = poll_all(
        db_session,
        read_neighbors=lambda nas: [{"identity": "GBB", "interface": "sfp1"}],
        read_router_neighbors=router_read,
        now=NOW,
    )
    assert r["nas_polled"] == 1
    assert r["via_nas"] == 1
    assert r["via_router"] == 0
    assert r["created"] == 1


def test_nas_without_api_url_polls_via_linked_router(db_session):
    spdc, nas_spdc = _nas_node(db_session, "SPDC Access", api_url=None)
    _plain(db_session, "GBB")
    router = _router_for(db_session, nas_spdc, "spdc-rtr")

    seen = []

    def router_read(r):
        seen.append(r.id)
        return [{"identity": "GBB", "interface": "sfp1"}]

    r = poll_all(
        db_session,
        read_neighbors=_raise_nas_read,
        read_router_neighbors=router_read,
        now=NOW,
    )
    assert seen == [router.id]
    assert r["nas_polled"] == 1
    assert r["via_router"] == 1
    assert r["via_nas"] == 0
    assert r["nas_failed"] == 0
    assert r["created"] == 1


def test_nas_without_api_url_inactive_router_skipped(db_session):
    _, nas = _nas_node(db_session, "Orphan Access", api_url=None)
    _router_for(db_session, nas, "orphan-rtr", is_active=False)

    r = poll_all(
        db_session,
        read_neighbors=_raise_nas_read,
        read_router_neighbors=_raise_nas_read,
        now=NOW,
    )
    assert r["skipped_no_creds"] == 1
    assert r["nas_polled"] == 0
    assert r["nas_failed"] == 0


def test_nas_with_no_creds_at_all_counted_not_raised(db_session):
    _nas_node(db_session, "Bare Access", api_url=None)  # no router row at all

    r = poll_all(
        db_session,
        read_neighbors=_raise_nas_read,
        read_router_neighbors=_raise_nas_read,
        now=NOW,
    )
    assert r["skipped_no_creds"] == 1
    assert r["nas_failed"] == 0
    assert r["nas_polled"] == 0


def test_jump_host_router_skipped_with_distinct_counter(db_session):
    _, nas = _nas_node(db_session, "Remote Access", api_url=None)
    _router_for(
        db_session,
        nas,
        "remote-rtr",
        access_method=RouterAccessMethod.jump_host,
    )

    r = poll_all(
        db_session,
        read_neighbors=_raise_nas_read,
        read_router_neighbors=_raise_nas_read,
        now=NOW,
    )
    assert r["skipped_jump_host"] == 1
    assert r["skipped_no_creds"] == 0
    assert r["nas_failed"] == 0
    assert r["nas_polled"] == 0


def test_router_fallback_failure_isolated(db_session):
    ok, nas_ok = _nas_node(db_session, "OK Access")  # NAS path, api_url set
    bad, nas_bad = _nas_node(db_session, "Karsana Access", api_url=None)
    _router_for(db_session, nas_bad, "karsana-rtr")
    _plain(db_session, "GBB")

    def router_read(router):
        raise OSError("unreachable via router")

    r = poll_all(
        db_session,
        read_neighbors=lambda nas: [{"identity": "GBB", "interface": "sfp1"}],
        read_router_neighbors=router_read,
        now=NOW,
    )
    assert r["nas_failed"] == 1  # router-path failure isolated, run continues
    assert r["nas_polled"] == 1
    assert r["via_nas"] == 1
    assert r["created"] == 1
