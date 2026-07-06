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

from app.models.network_monitoring import NetworkDevice, NetworkTopologyLink
from app.models.router_management import Router
from app.services.topology import lldp_poller
from app.services.topology.lldp_poller import SOURCE, poll_all

NOW = datetime(2026, 6, 17, 14, 0, tzinfo=UTC)


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
    def __init__(self, rows):
        self._rows = rows

    def get(self):
        return self._rows


class _FakeApi:
    def __init__(self, rows):
        self._rows = rows

    def get_resource(self, path):
        assert path == "/ip/neighbor", path
        return _FakeResource(self._rows)


class _FakePool:
    """Stand-in for RouterOsApiPool: records lifecycle for hygiene assertions."""

    def __init__(self, rows=None, get_api_error=None):
        self._rows = rows or []
        self._get_api_error = get_api_error
        self.disconnected = False
        self.timeout = None

    def set_timeout(self, socket_timeout):
        self.timeout = socket_timeout

    def get_api(self):
        if self._get_api_error is not None:
            raise self._get_api_error
        return _FakeApi(self._rows)

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
