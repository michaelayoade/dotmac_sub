"""Topology live-status warmer (Phase 3, P3.2)."""

from __future__ import annotations

from app.models.network_monitoring import NetworkDevice
from app.services.topology.live_status import warm_topology_status


class _FakeClient:
    def __init__(self, hosts, triggers):
        self._hosts = hosts
        self._triggers = triggers

    def get_hosts(self, host_ids=None, **_kw):
        return self._hosts

    def get_triggers(self, host_ids=None, active_only=True, limit=100, **_kw):
        return self._triggers


def _node(hostid, name):
    return NetworkDevice(
        name=name,
        source="zabbix_reconcile",
        zabbix_hostid=hostid,
        is_active=True,
    )


def test_warm_sets_live_status_per_node(db_session):
    db_session.add_all(
        [
            _node("1", "up-node"),
            _node("2", "down-node"),
            _node("3", "problem-node"),
            _node("4", "unknown-node"),
            # not reconciled -> must be ignored by the warmer
            NetworkDevice(name="other", source="splynx", is_active=True),
        ]
    )
    db_session.flush()

    hosts = [
        {"hostid": "1", "available": "1", "interfaces": []},
        {"hostid": "2", "available": "2", "interfaces": []},
        {"hostid": "3", "available": "1", "interfaces": []},
        {"hostid": "4", "available": "0", "interfaces": []},
    ]
    triggers = [{"triggerid": "t1", "value": 1, "hosts": [{"hostid": "3"}]}]

    result = warm_topology_status(db_session, _FakeClient(hosts, triggers))
    assert result["nodes"] == 4

    by_host = {
        n.zabbix_hostid: n.live_status
        for n in db_session.query(NetworkDevice)
        .filter(NetworkDevice.zabbix_hostid.isnot(None))
        .all()
    }
    assert by_host["1"] == "up"
    assert by_host["2"] == "down"
    assert by_host["3"] == "problem"
    assert by_host["4"] == "unknown"
    # all warmed nodes got a timestamp
    warmed = (
        db_session.query(NetworkDevice)
        .filter(NetworkDevice.live_status.isnot(None))
        .all()
    )
    assert len(warmed) == 4
    assert all(n.live_status_at is not None for n in warmed)


def test_down_beats_problem(db_session):
    # An unavailable host with an active trigger is 'down', not 'problem'.
    db_session.add(_node("9", "n9"))
    db_session.flush()
    hosts = [{"hostid": "9", "available": "2", "interfaces": []}]
    triggers = [{"triggerid": "t", "value": 1, "hosts": [{"hostid": "9"}]}]
    warm_topology_status(db_session, _FakeClient(hosts, triggers))
    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="9").one()
    assert n.live_status == "down"


def test_interface_availability_fallback(db_session):
    # No host-level 'available'; derive from the main interface.
    db_session.add(_node("7", "n7"))
    db_session.flush()
    hosts = [
        {
            "hostid": "7",
            "available": "0",
            "interfaces": [{"main": "1", "available": "2"}],
        }
    ]
    warm_topology_status(db_session, _FakeClient(hosts, []))
    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="7").one()
    assert n.live_status == "down"
