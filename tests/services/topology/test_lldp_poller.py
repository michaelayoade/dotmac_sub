"""poll_all upsert + prune + idempotency + failure isolation (Phase 2, P2.4).

Neighbors are read over the RouterOS binary API (port 8728); the fetch is faked
with an injected pool/api whose ``get_resource('/ip/neighbor').get()`` returns
sample dicts in the real shape the fleet returns.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from routeros_api.exceptions import (
    RouterOsApiCommunicationError,
    RouterOsApiConnectionError,
    RouterOsApiParsingError,
)

from app.models.network_monitoring import NetworkDevice, NetworkTopologyLink
from app.models.router_management import Router
from app.services.topology import lldp_poller
from app.services.topology.lldp_poller import SOURCE, poll_all

NOW = datetime(2026, 6, 17, 14, 0, tzinfo=UTC)


def _empty_table_error():
    """The exact parse failure routeros_api raises for an empty neighbor table:
    a bare '!empty' sentence that some library builds refuse to parse."""
    return RouterOsApiParsingError("Malformed sentence %s", [b"!empty", b".tag=2"])


def _router_node(db, name, mgmt_ip=None, is_active=True, network_device=True):
    """A router row + its network_device node (the local end of edges)."""
    node = None
    if network_device:
        node = NetworkDevice(
            name=name,
            mgmt_ip=mgmt_ip,
            source="zabbix_reconcile",
            is_active=True,
        )
        db.add(node)
        db.flush()
    router = Router(
        name=name,
        hostname=name,
        management_ip=mgmt_ip or "10.20.0.1",
        rest_api_username="api-user",
        rest_api_password="api-pass",
        network_device_id=node.id if node else None,
        is_active=is_active,
    )
    db.add(router)
    db.flush()
    return node, router


def _plain(db, name, mgmt_ip=None):
    """A network_device with no router (a switch/aggregation seen as a neighbor)."""
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


# --- Fake routeros_api pool/api ---------------------------------------------


class _FakeResource:
    def __init__(self, rows, get_error=None):
        self._rows = rows
        self._get_error = get_error

    def get(self):
        if self._get_error is not None:
            raise self._get_error
        return self._rows


class _FakeApi:
    def __init__(self, rows, get_error=None):
        self._rows = rows
        self._get_error = get_error

    def get_resource(self, path):
        assert path == "/ip/neighbor", path
        return _FakeResource(self._rows, get_error=self._get_error)


class _FakePool:
    """Stand-in for RouterOsApiPool: records lifecycle for hygiene assertions."""

    def __init__(self, rows=None, get_api_error=None, get_error=None):
        self._rows = rows or []
        self._get_api_error = get_api_error
        self._get_error = get_error
        self.disconnected = False
        self.timeout = None

    def set_timeout(self, socket_timeout):
        self.timeout = socket_timeout

    def get_api(self):
        if self._get_api_error is not None:
            raise self._get_api_error
        return _FakeApi(self._rows, get_error=self._get_error)

    def disconnect(self):
        self.disconnected = True


# --- Edge building over the binary-API fetch ---------------------------------


def test_poll_all_upsert_idempotent_and_prune(db_session):
    _spdc, r_spdc = _router_node(db_session, "SPDC Access")
    _gbb, r_gbb = _router_node(db_session, "GBB")
    _switch = _plain(db_session, "SPDC-Switch", mgmt_ip="10.0.0.77")

    neighbors = {
        str(r_spdc.id): [
            {
                "identity": "GBB",
                "interface": "sfp-sfpplus1=>GBB Fiber",
                "platform": "MikroTik",
            },
            {"identity": "", "interface": "ether5", "address": "192.168.88.50"},  # CPE
            {
                "identity": "sw",
                "address": "10.0.0.77",
                "interface": "ether2",
            },  # switch by IP
        ],
        str(r_gbb.id): [
            {"identity": "SPDC Access", "interface": "sfp1"}
        ],  # sees spdc back
    }
    stub = lambda router: neighbors.get(str(router.id), [])  # noqa: E731

    r1 = poll_all(db_session, read_neighbors=stub, now=NOW)
    assert r1["routers_polled"] == 2
    assert r1["via_binary_api"] == 2
    assert r1["neighbors_seen"] == 4
    assert r1["created"] == 2  # spdc<->gbb (deduped) + spdc<->switch
    assert len(_active_links(db_session)) == 2

    # Interface + medium captured from the binary-API 'interface' string.
    fiber = [_l for _l in _active_links(db_session) if _l.medium.value == "fiber"]
    assert fiber, "sfp interface should classify as fiber"
    assert fiber[0].metadata_["local_interface"].startswith("sfp")

    # --- idempotent: run again, no new rows, only last_seen_at bumps ---
    r2 = poll_all(
        db_session, read_neighbors=stub, now=datetime(2026, 6, 17, 14, 5, tzinfo=UTC)
    )
    assert r2["created"] == 0
    assert r2["updated"] == 2
    assert r2["pruned"] == 0
    assert len(_active_links(db_session)) == 2

    # --- prune: spdc stops seeing the switch -> that edge soft-pruned ---
    neighbors[str(r_spdc.id)] = [{"identity": "GBB", "interface": "sfp1"}]
    r3 = poll_all(
        db_session, read_neighbors=stub, now=datetime(2026, 6, 17, 14, 10, tzinfo=UTC)
    )
    assert r3["pruned"] == 1
    assert len(_active_links(db_session)) == 1  # only spdc<->gbb remains
    inactive = (
        db_session.query(NetworkTopologyLink)
        .filter(
            NetworkTopologyLink.source == SOURCE,
            NetworkTopologyLink.is_active.is_(False),
        )
        .all()
    )
    assert len(inactive) == 1


def test_edge_survives_when_its_observing_router_fails(db_session):
    """An existing edge must NOT be pruned on a run where the router that
    observes it failed (or was skipped). Only edges a SUCCESSFULLY-polled router
    could have re-observed are prune-eligible — otherwise the routers that
    routinely time out would flap their edges active/inactive every run."""
    _spdc, r_spdc = _router_node(db_session, "SPDC Access")
    _gbb, r_gbb = _router_node(db_session, "GBB")
    switch = _plain(db_session, "SPDC-Switch", mgmt_ip="10.0.0.77")

    neighbors = {
        # spdc is the ONLY observer of the spdc<->switch edge
        str(r_spdc.id): [
            {"identity": "GBB", "interface": "sfp1"},
            {"identity": "sw", "address": "10.0.0.77", "interface": "ether2"},
        ],
        str(r_gbb.id): [{"identity": "SPDC Access", "interface": "sfp1"}],
    }

    def healthy(router):
        return neighbors.get(str(router.id), [])

    r1 = poll_all(db_session, read_neighbors=healthy, now=NOW)
    assert r1["created"] == 2  # spdc<->gbb + spdc<->switch
    assert len(_active_links(db_session)) == 2

    # --- spdc (the switch edge's only observer) times out this run ---
    def spdc_fails(router):
        if router.id == r_spdc.id:
            raise OSError("routeros connect timeout")
        return neighbors.get(str(router.id), [])

    r2 = poll_all(
        db_session,
        read_neighbors=spdc_fails,
        now=datetime(2026, 6, 17, 14, 10, tzinfo=UTC),
    )
    assert r2["routers_failed"] == 1
    # No edge is pruned: the switch edge's only observer failed, and the
    # spdc<->gbb edge was re-seen from gbb's side.
    assert r2["pruned"] == 0
    assert len(_active_links(db_session)) == 2
    switch_links = [
        _l
        for _l in _active_links(db_session)
        if switch.id in (_l.source_device_id, _l.target_device_id)
    ]
    assert len(switch_links) == 1, "spdc<->switch edge must survive spdc's failure"


def test_neighbor_matched_by_identity_then_address(db_session):
    """Identity wins when present; address is the fallback key."""
    _spdc, r_spdc = _router_node(db_session, "SPDC Access")
    _gbb = _plain(db_session, "GBB", mgmt_ip="10.9.9.9")  # matchable by name
    _core = _plain(db_session, "Core Router", mgmt_ip="10.0.0.1")

    neighbors = {
        str(r_spdc.id): [
            # identity match (name), even though address is unknown
            {"identity": "GBB", "address": "203.0.113.5", "interface": "sfp1"},
            # no identity -> falls back to address match against Core Router mgmt_ip
            {"identity": "", "address": "10.0.0.1", "interface": "ether3"},
        ]
    }
    stub = lambda router: neighbors.get(str(router.id), [])  # noqa: E731

    r = poll_all(db_session, read_neighbors=stub, now=NOW)
    assert r["created"] == 2  # spdc<->gbb (by name) + spdc<->core (by address)
    remotes = {link.metadata_["remote_identity"] for link in _active_links(db_session)}
    assert "GBB" in remotes  # identity-matched edge kept the identity


def test_unreachable_router_isolated(db_session):
    _ok, r_ok = _router_node(db_session, "OK Access")
    _bad, r_bad = _router_node(db_session, "Karsana Access")
    _plain(db_session, "GBB")

    def stub(router):
        if router.id == r_bad.id:
            raise OSError("routeros connect timeout")
        return [{"identity": "GBB", "interface": "sfp1"}]

    r = poll_all(db_session, read_neighbors=stub, now=NOW)
    assert r["routers_failed"] == 1
    assert r["routers_polled"] == 1  # the reachable one still processed
    assert r["created"] == 1


def test_router_without_network_device_skipped(db_session):
    _router_node(db_session, "Orphan Access", network_device=False)

    def stub(router):
        raise AssertionError("must not fetch a router with no network_device_id")

    r = poll_all(db_session, read_neighbors=stub, now=NOW)
    assert r["skipped_no_device"] == 1
    assert r["routers_polled"] == 0
    assert r["routers_failed"] == 0


def test_inactive_router_not_polled(db_session):
    _node, _router = _router_node(db_session, "Down Access", is_active=False)
    _plain(db_session, "GBB")

    def stub(router):
        raise AssertionError("inactive routers must not be polled")

    r = poll_all(db_session, read_neighbors=stub, now=NOW)
    assert r["routers_polled"] == 0
    assert r["created"] == 0


# --- Connection hygiene (binary-API fetch) -----------------------------------


def _fake_router():
    return SimpleNamespace(
        name="edge-rtr",
        management_ip="10.20.0.1",
        rest_api_username="api-user",
        rest_api_password="api-pass",
    )


def test_binary_fetch_returns_dicts_and_connects_on_8728():
    rows = [
        {
            "identity": "AFR Access",
            "address": "172.16.115.66",
            "interface": "sfp-sfpplus3=>AFR Fiber",
            "mac-address": "AA:BB:CC:DD:EE:FF",
            "platform": "MikroTik",
            "board-name": "CCR2004",
        }
    ]
    pool = _FakePool(rows=rows)
    captured = {}

    def factory(host, **kwargs):
        captured["host"] = host
        captured.update(kwargs)
        return pool

    out = lldp_poller._read_neighbors_via_binary_api(
        _fake_router(), pool_factory=factory
    )

    assert out == rows
    assert out[0]["identity"] == "AFR Access"
    assert captured["host"] == "10.20.0.1"
    assert captured["port"] == 8728
    assert captured["plaintext_login"] is True
    assert captured["username"] == "api-user"  # decrypt falls back to raw legacy creds
    assert pool.timeout == lldp_poller.ROUTER_SOCKET_TIMEOUT
    assert pool.disconnected is True  # released on the happy path too


def test_binary_fetch_disconnects_even_when_get_api_fails():
    """No 8728 session leak: get_api() blowing up still disconnects the pool."""
    pool = _FakePool(get_api_error=OSError("login failed"))

    with pytest.raises(OSError):
        lldp_poller._read_neighbors_via_binary_api(
            _fake_router(), pool_factory=lambda *a, **k: pool
        )

    assert pool.disconnected is True


def test_binary_fetch_end_to_end_via_default_path(db_session):
    """poll_all driving the real fetch helper with an injected pool_factory."""
    _spdc, _r = _router_node(db_session, "SPDC Access")
    _plain(db_session, "GBB")

    pool = _FakePool(rows=[{"identity": "GBB", "interface": "sfp-sfpplus1"}])

    def read(router):
        return lldp_poller._read_neighbors_via_binary_api(
            router, pool_factory=lambda *a, **k: pool
        )

    r = poll_all(db_session, read_neighbors=read, now=NOW)
    assert r["created"] == 1
    assert r["via_binary_api"] == 1
    assert pool.disconnected is True


# --- Soft time limit + wall-clock budget --------------------------------------


def test_soft_time_limit_propagates(db_session):
    _router_node(db_session, "SPDC Access")

    def reader(router):
        raise SoftTimeLimitExceeded()

    with pytest.raises(SoftTimeLimitExceeded):
        poll_all(db_session, read_neighbors=reader, now=NOW)


def test_time_budget_exhaustion_skips_remainder_without_failing(db_session):
    _router_node(db_session, "A Access")
    _router_node(db_session, "B Access")
    _plain(db_session, "GBB")

    def reader(router):
        time.sleep(0.05)
        return [{"identity": "GBB", "interface": "sfp1"}]

    r = poll_all(db_session, read_neighbors=reader, now=NOW, time_budget_seconds=0.02)
    assert r["routers_polled"] == 1  # first router attempted before budget tripped
    assert r["skipped_time_budget"] == 1  # remainder skipped, not failed
    assert r["routers_failed"] == 0
    assert r["created"] == 1  # run still reconciles what it saw


# --- Empty neighbor table (robustness: '!empty' parse crash) ------------------


def test_empty_neighbor_table_yields_no_neighbors():
    """A router with an EMPTY neighbor table raises routeros_api's '!empty' parse
    error; the fetch swallows it as 0 neighbors and still disconnects cleanly."""
    pool = _FakePool(get_error=_empty_table_error())
    out = lldp_poller._read_neighbors_via_binary_api(
        _fake_router(), pool_factory=lambda *a, **k: pool
    )
    assert out == []
    assert pool.disconnected is True  # no session leak on the empty path


def test_non_empty_parse_error_still_propagates():
    """Only the '!empty' marker is swallowed; a genuinely malformed sentence
    (a real bug/corruption) must still surface as a router failure."""
    pool = _FakePool(
        get_error=RouterOsApiParsingError("Malformed attribute %s", b"=garbage")
    )
    with pytest.raises(RouterOsApiParsingError):
        lldp_poller._read_neighbors_via_binary_api(
            _fake_router(), pool_factory=lambda *a, **k: pool
        )
    assert pool.disconnected is True


def test_poll_all_empty_table_not_counted_as_failure(db_session):
    """End-to-end: an empty table is 0 neighbors, NOT routers_failed."""
    _node, _router = _router_node(db_session, "SPDC Access")
    _plain(db_session, "GBB")

    pool = _FakePool(get_error=_empty_table_error())

    def read(router):
        return lldp_poller._read_neighbors_via_binary_api(
            router, pool_factory=lambda *a, **k: pool
        )

    r = poll_all(db_session, read_neighbors=read, now=NOW)
    assert r["routers_failed"] == 0
    assert r["routers_polled"] == 1
    assert r["via_binary_api"] == 1
    assert r["neighbors_seen"] == 0
    assert r["created"] == 0
    assert len(_active_links(db_session)) == 0


# --- REST (443) fallback for REST-only cores (8728 filtered) ------------------


def _conn_error():
    """A binary-API CONNECTION-class failure (8728 filtered/timeout/refused)."""
    return RouterOsApiConnectionError("timed out")


def _auth_error():
    """A binary-API AUTH failure (bad creds / device-side RADIUS reject).

    routeros_api surfaces a login trap as RouterOsApiCommunicationError, which is
    deliberately NOT a connection-class error -> no REST fallback."""
    return RouterOsApiCommunicationError("cannot log in", b"!trap")


def test_dispatch_falls_back_to_rest_on_binary_connection_failure():
    """8728 filtered (Garki Core / Abuja Medallion): binary connection failure
    falls back to REST(443), which returns the parsed neighbors."""
    rest_rows = [{"identity": "Abuja Medallion Peer", "interface": "sfp-sfpplus1"}]

    def binary(router):
        raise _conn_error()

    def rest(router):
        return rest_rows

    out, transport = lldp_poller._read_neighbors(
        _fake_router(), binary_reader=binary, rest_reader=rest
    )
    assert transport == "rest"
    assert out == rest_rows


def test_dispatch_does_not_fall_back_on_auth_failure():
    """An AUTH failure (Kubwa-style RADIUS reject) must NOT retry over REST —
    REST hits the same auth backend. The error propagates; REST is never called."""
    rest_called = {"n": 0}

    def binary(router):
        raise _auth_error()

    def rest(router):
        rest_called["n"] += 1
        return [{"identity": "should-not-happen"}]

    with pytest.raises(RouterOsApiCommunicationError):
        lldp_poller._read_neighbors(
            _fake_router(), binary_reader=binary, rest_reader=rest
        )
    assert rest_called["n"] == 0


def test_dispatch_binary_success_never_calls_rest():
    """When 8728 answers, the REST path is never touched."""
    rest_called = {"n": 0}

    def binary(router):
        return [{"identity": "GBB", "interface": "sfp1"}]

    def rest(router):
        rest_called["n"] += 1
        return []

    out, transport = lldp_poller._read_neighbors(
        _fake_router(), binary_reader=binary, rest_reader=rest
    )
    assert transport == "binary"
    assert out == [{"identity": "GBB", "interface": "sfp1"}]
    assert rest_called["n"] == 0


def test_rest_reader_parses_json_array_into_neighbor_dict_shape(monkeypatch):
    """REST returns /ip/neighbor as a JSON array with the hyphenated fields; the
    reader yields the SAME dict shape the binary reader does (address6/mac/
    interface/board-name preserved) so downstream matching is unchanged."""
    rest_json = [
        {
            "identity": "Garki Core",
            "address": "160.119.127.252",
            "address4": "160.119.127.252",
            "address6": "fe80::baxa",
            "mac-address": "48:8F:5A:11:22:33",
            "interface": "sfp-sfpplus2",
            "board-name": "CCR1072-1G-8S+",
            "platform": "MikroTik",
        }
    ]
    captured = {}

    def fake_execute(router, method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["kwargs"] = kwargs
        return rest_json

    import app.services.router_management.connection as conn

    monkeypatch.setattr(conn.RouterConnectionService, "execute", fake_execute)

    out = lldp_poller._read_neighbors_via_rest(_fake_router())

    assert out == rest_json
    assert out[0]["mac-address"] == "48:8F:5A:11:22:33"
    assert out[0]["address6"] == "fe80::baxa"
    assert out[0]["interface"] == "sfp-sfpplus2"
    assert out[0]["board-name"] == "CCR1072-1G-8S+"
    # Reused the established REST layer: GET /ip/neighbor with discovery-grade
    # tunables (one attempt, ~12s read bound).
    assert (captured["method"], captured["path"]) == ("GET", "/ip/neighbor")
    assert captured["kwargs"]["max_retries"] == lldp_poller.ROUTER_REST_MAX_RETRIES
    assert captured["kwargs"]["read_timeout"] == lldp_poller.ROUTER_REST_READ_TIMEOUT


def test_poll_all_rest_fallback_end_to_end(db_session):
    """poll_all via the real dispatcher: binary connection-fails, REST supplies
    the neighbors, and the run counts the read as via_rest (not routers_failed)."""
    _node, _r = _router_node(db_session, "Garki Core")
    _plain(db_session, "GBB")

    def binary(router):
        raise _conn_error()

    def rest(router):
        return [{"identity": "GBB", "interface": "sfp-sfpplus1"}]

    def read(router):
        return lldp_poller._read_neighbors(
            router, binary_reader=binary, rest_reader=rest
        )

    r = poll_all(db_session, read_neighbors=read, now=NOW)
    assert r["via_rest"] == 1
    assert r["via_binary_api"] == 0
    assert r["routers_failed"] == 0
    assert r["created"] == 1


def test_poll_all_auth_failure_counts_as_failed_no_rest(db_session):
    """An AUTH failure end-to-end: no REST fallback, counted as routers_failed."""
    _node, _r = _router_node(db_session, "Kubwa Core")
    _plain(db_session, "GBB")
    rest_called = {"n": 0}

    def binary(router):
        raise _auth_error()

    def rest(router):
        rest_called["n"] += 1
        return []

    def read(router):
        return lldp_poller._read_neighbors(
            router, binary_reader=binary, rest_reader=rest
        )

    r = poll_all(db_session, read_neighbors=read, now=NOW)
    assert r["routers_failed"] == 1
    assert r["via_rest"] == 0
    assert r["via_binary_api"] == 0
    assert r["created"] == 0
    assert rest_called["n"] == 0


# --- Smarter matcher (identity aliases, mgmt_ip, fe80::, ambiguity) -----------


def test_matcher_identity_alias_mgmt_ip_and_ambiguity(db_session):
    """The advertised /system identity often differs from the modeled device
    name; the fuzzy fallback resolves those aliases, mgmt_ip address matches
    work, and ambiguous stripped forms are NOT guessed."""
    _node, r_edge = _router_node(db_session, "Edge Access")
    _plain(db_session, "Garki Core")  # <- identity "Abuja Core I Garki"
    _plain(db_session, "BOI Asokoro")  # <- identity "BOI Asokoro Access"
    _plain(db_session, "Airport Switch", mgmt_ip="172.16.151.2")  # by address
    # Two devices collapse to the same stripped form -> ambiguous -> no match.
    _plain(db_session, "Kubwa Core")
    _plain(db_session, "Kubwa Switch")

    neighbors = {
        str(r_edge.id): [
            {"identity": "Abuja Core I Garki", "interface": "sfp1"},
            {"identity": "BOI Asokoro Access", "interface": "sfp2"},
            {"identity": "sw", "address": "172.16.151.2", "interface": "ether3"},
            {"identity": "Kubwa Router", "interface": "ether4"},  # ambiguous
        ]
    }
    stub = lambda router: neighbors.get(str(router.id), [])  # noqa: E731

    r = poll_all(db_session, read_neighbors=stub, now=NOW)
    assert r["created"] == 3  # garki + boi (stripped) + airport (address)
    assert r["matched_by_stripped_identity"] == 2
    assert r["matched_by_address"] == 1
    remotes = {link.metadata_["remote_identity"] for link in _active_links(db_session)}
    assert "Abuja Core I Garki" in remotes
    assert "BOI Asokoro Access" in remotes
    assert "Kubwa Router" not in remotes  # ambiguity never guessed


def test_fe80_only_neighbor_matches_by_identity(db_session):
    """A neighbor whose ONLY advertised address is an IPv6 link-local (fe80::)
    must not be dropped on the address step — it falls through to identity."""
    _node, r_edge = _router_node(db_session, "Edge Access")
    _plain(db_session, "GBB", mgmt_ip="10.9.9.9")

    neighbors = {
        str(r_edge.id): [
            # fe80:: sits in the 'address' field: it must be ignored (not tried
            # as an IP) and identity 'GBB' must still resolve the edge.
            {"identity": "GBB", "address": "fe80::1", "interface": "sfp1"},
        ]
    }
    stub = lambda router: neighbors.get(str(router.id), [])  # noqa: E731

    r = poll_all(db_session, read_neighbors=stub, now=NOW)
    assert r["created"] == 1
    assert r["matched_by_identity"] == 1
    assert r["matched_by_address"] == 0


# --- No duplicate of the manually-modeled backbone ----------------------------


def test_manual_backbone_link_not_duplicated_and_survives(db_session):
    """A canonical pair already carrying an ACTIVE manual (non-lldp) link must
    not get a SECOND lldp row; the manual link stays authoritative and the
    source='lldp_neighbor' soft-prune never touches it."""
    node_a, r_a = _router_node(db_session, "Abuja Core")
    node_b, r_b = _router_node(db_session, "Lagos Core")
    # A device pair modeled by hand with source='manual', plus a NULL-source one.
    node_c, r_c = _router_node(db_session, "Wuse Core")
    node_d = _plain(db_session, "Ikeja Core")

    manual = NetworkTopologyLink(
        source_device_id=node_a.id,
        target_device_id=node_b.id,
        source="manual",
        topology_group="abuja-backbone",
        is_active=True,
        discovered_at=NOW,
    )
    manual_null = NetworkTopologyLink(
        source_device_id=node_c.id,
        target_device_id=node_d.id,
        source=None,  # operator insert with no source stamped
        topology_group="lagos-backbone",
        is_active=True,
        discovered_at=NOW,
    )
    db_session.add_all([manual, manual_null])
    db_session.flush()

    neighbors = {
        # Both endpoints rediscover the manually-modeled pair (either direction).
        str(r_a.id): [{"identity": "Lagos Core", "interface": "sfp1"}],
        str(r_b.id): [{"identity": "Abuja Core", "interface": "sfp1"}],
        str(r_c.id): [{"identity": "Ikeja Core", "interface": "sfp1"}],
    }
    stub = lambda router: neighbors.get(str(router.id), [])  # noqa: E731

    r = poll_all(db_session, read_neighbors=stub, now=NOW)
    assert r["created"] == 0  # neither manual pair duplicated
    assert r["skipped_manual_dup"] == 2

    # No lldp row exists for either canonical pair; the manual links survive.
    assert len(_active_links(db_session)) == 0  # source='lldp_neighbor' rows
    db_session.refresh(manual)
    db_session.refresh(manual_null)
    assert manual.is_active is True and manual.source == "manual"
    assert manual_null.is_active is True and manual_null.source is None

    # A second run (prune pass) must still leave the manual links untouched.
    r2 = poll_all(
        db_session, read_neighbors=stub, now=datetime(2026, 6, 17, 14, 5, tzinfo=UTC)
    )
    assert r2["pruned"] == 0
    db_session.refresh(manual)
    assert manual.is_active is True
