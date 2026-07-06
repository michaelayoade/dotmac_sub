"""Topology live-status warmer (Phase 3, P3.2)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.catalog import NasDevice
from app.models.network_monitoring import NetworkDevice
from app.models.radius_active_session import RadiusActiveSession
from app.services.topology.live_status import warm_topology_status


class _FakeClient:
    def __init__(self, hosts, triggers, items=None):
        self._hosts = hosts
        self._triggers = triggers
        self._items = items or []

    def get_hosts(self, host_ids=None, **_kw):
        return self._hosts

    def get_triggers(self, host_ids=None, active_only=True, limit=100, **_kw):
        return self._triggers

    def get_items(self, host_ids=None, metric=None, limit=100000, **_kw):
        if host_ids is None:
            return self._items
        wanted = {str(host_id) for host_id in host_ids}
        return [item for item in self._items if str(item.get("hostid")) in wanted]


def _node(hostid, name):
    return NetworkDevice(
        name=name,
        source="zabbix_reconcile",
        zabbix_hostid=hostid,
        is_active=True,
    )


def _seed_live_session(db_session, device):
    """Attach a NAS carrying one live RADIUS session to ``device``.

    Mirrors the real data path: radius_active_sessions.nas_device_id ->
    nas_devices.id -> nas_devices.network_device_id -> network_devices.id.
    """
    nas = NasDevice(name=f"nas-{device.zabbix_hostid}", network_device_id=device.id)
    db_session.add(nas)
    db_session.flush()
    db_session.add(
        RadiusActiveSession(
            nas_device_id=nas.id,
            username=f"cust-{device.zabbix_hostid}",
            acct_session_id=f"sess-{device.zabbix_hostid}",
            session_start=datetime.now(UTC),
        )
    )
    db_session.flush()
    return nas


def test_warm_sets_live_status_per_node(db_session):
    db_session.add_all(
        [
            _node("1", "up-node"),
            _node("2", "down-node"),
            _node("3", "triggered-but-reachable-node"),
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
    assert by_host["3"] == "up"
    assert by_host["4"] == "unknown"
    # all warmed nodes got a timestamp
    warmed = (
        db_session.query(NetworkDevice)
        .filter(NetworkDevice.live_status.isnot(None))
        .all()
    )
    assert len(warmed) == 4
    assert all(n.live_status_at is not None for n in warmed)


def test_down_status_ignores_active_triggers(db_session):
    # Active triggers no longer drive live_status; host availability does.
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


def test_icmp_fallback_when_snmp_availability_unknown(db_session):
    db_session.add_all([_node("10", "ping-up"), _node("11", "ping-down")])
    db_session.flush()
    hosts = [
        {"hostid": "10", "available": "0", "status": "0", "interfaces": []},
        {"hostid": "11", "available": "0", "status": "0", "interfaces": []},
    ]
    items = [
        {"hostid": "10", "key_": "icmpping", "lastvalue": "1"},
        {"hostid": "11", "key_": "icmpping", "lastvalue": "0"},
    ]

    warm_topology_status(db_session, _FakeClient(hosts, [], items))

    by_host = {
        n.zabbix_hostid: n.live_status
        for n in db_session.query(NetworkDevice)
        .filter(NetworkDevice.zabbix_hostid.in_(["10", "11"]))
        .all()
    }
    assert by_host["10"] == "up"
    assert by_host["11"] == "down"


def test_icmp_item_wins_over_snmp_availability(db_session):
    db_session.add_all(
        [_node("20", "snmp-up-icmp-down"), _node("21", "snmp-down-icmp-up")]
    )
    db_session.flush()
    hosts = [
        {"hostid": "20", "available": "1", "status": "0", "interfaces": []},
        {"hostid": "21", "available": "2", "status": "0", "interfaces": []},
    ]
    items = [
        {"hostid": "20", "key_": "icmpping", "lastvalue": "0"},
        {"hostid": "21", "key_": "icmpping", "lastvalue": "1"},
    ]

    warm_topology_status(db_session, _FakeClient(hosts, [], items))

    by_host = {
        n.zabbix_hostid: n.live_status
        for n in db_session.query(NetworkDevice)
        .filter(NetworkDevice.zabbix_hostid.in_(["20", "21"]))
        .all()
    }
    assert by_host["20"] == "down"
    assert by_host["21"] == "up"


def test_any_failed_icmp_item_wins(db_session):
    db_session.add(_node("23", "mixed-icmp"))
    db_session.flush()
    hosts = [{"hostid": "23", "available": "1", "status": "0", "interfaces": []}]
    items = [
        {"hostid": "23", "key_": "icmpping", "lastvalue": "1"},
        {"hostid": "23", "key_": "icmpping", "lastvalue": "0"},
        {"hostid": "23", "key_": "icmpping", "lastvalue": "1"},
    ]

    warm_topology_status(db_session, _FakeClient(hosts, [], items))

    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="23").one()
    assert n.live_status == "down"


def test_snmp_availability_used_when_icmp_item_missing(db_session):
    db_session.add(_node("22", "snmp-only"))
    db_session.flush()
    hosts = [{"hostid": "22", "available": "1", "status": "0", "interfaces": []}]

    warm_topology_status(db_session, _FakeClient(hosts, [], items=[]))

    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="22").one()
    assert n.live_status == "up"


def test_icmp_fallback_ignored_for_disabled_host(db_session):
    db_session.add(_node("12", "disabled"))
    db_session.flush()
    hosts = [{"hostid": "12", "available": "0", "status": "1", "interfaces": []}]
    items = [{"hostid": "12", "key_": "icmpping", "lastvalue": "1"}]

    warm_topology_status(db_session, _FakeClient(hosts, [], items))

    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="12").one()
    assert n.live_status == "unknown"


def test_icmp_failure_marks_disabled_host_down(db_session):
    db_session.add(_node("13", "disabled-down"))
    db_session.flush()
    hosts = [{"hostid": "13", "available": "0", "status": "1", "interfaces": []}]
    items = [{"hostid": "13", "key_": "icmpping", "lastvalue": "0"}]

    warm_topology_status(db_session, _FakeClient(hosts, [], items))

    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="13").one()
    assert n.live_status == "down"


def test_live_status_at_stamped_only_on_change(db_session):
    # live_status_at must mark when the node ENTERED its current state (the
    # dwell clock the customer-facing debounce relies on) — not every poll.
    db_session.add(_node("5", "n5"))
    db_session.flush()
    up = [{"hostid": "5", "available": "1", "interfaces": []}]
    down = [{"hostid": "5", "available": "2", "interfaces": []}]

    warm_topology_status(db_session, _FakeClient(up, []))
    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="5").one()
    # Re-read from the DB so first_at is in the same representation the later
    # refreshed comparison uses — SQLite drops tzinfo on round-trip, so an
    # in-session aware value would never equal the refreshed naive one. We are
    # asserting the stamp does not MOVE, not its tz-awareness.
    db_session.refresh(n)
    first_at = n.live_status_at

    # same status next poll -> timestamp does NOT move
    warm_topology_status(db_session, _FakeClient(up, []))
    db_session.refresh(n)
    assert n.live_status == "up"
    assert n.live_status_at == first_at

    # a real status change DOES move it
    n.live_status_at = datetime(2020, 1, 1, tzinfo=UTC)
    db_session.flush()
    warm_topology_status(db_session, _FakeClient(down, []))
    db_session.refresh(n)
    assert n.live_status == "down"
    assert n.live_status_at != datetime(2020, 1, 1, tzinfo=UTC)


def test_disabled_or_maintenance_host_is_unknown(db_session):
    # A host Zabbix isn't actively monitoring (disabled) or in maintenance must
    # not surface as "up" off a stale availability — it reads unknown.
    db_session.add_all([_node("10", "disabled"), _node("11", "maint")])
    db_session.flush()
    hosts = [
        {"hostid": "10", "available": "1", "status": "1", "interfaces": []},
        {
            "hostid": "11",
            "available": "1",
            "maintenance_status": "1",
            "interfaces": [],
        },
    ]
    warm_topology_status(db_session, _FakeClient(hosts, []))
    by_host = {
        n.zabbix_hostid: n.live_status
        for n in db_session.query(NetworkDevice).all()
        if n.zabbix_hostid
    }
    assert by_host["10"] == "unknown"
    assert by_host["11"] == "unknown"


def test_trapper_only_uisp_host_active_is_up(db_session):
    # A trapper-only UISP host: no polled interface, no icmpping, only a live
    # uisp.status="active" trapper -> coloured UP off the trapper fallback.
    db_session.add(_node("30", "onu-active"))
    db_session.flush()
    hosts = [{"hostid": "30", "available": "0", "status": "0", "interfaces": []}]
    items = [{"hostid": "30", "key_": "uisp.status", "lastvalue": "active"}]

    result = warm_topology_status(db_session, _FakeClient(hosts, [], items))

    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="30").one()
    assert n.live_status == "up"
    assert result["via_uisp_status"] == 1


def test_trapper_only_uisp_host_disconnected_is_down(db_session):
    db_session.add(_node("31", "onu-disconnected"))
    db_session.flush()
    hosts = [{"hostid": "31", "available": "0", "status": "0", "interfaces": []}]
    items = [{"hostid": "31", "key_": "uisp.status", "lastvalue": "disconnected"}]

    result = warm_topology_status(db_session, _FakeClient(hosts, [], items))

    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="31").one()
    assert n.live_status == "down"
    assert result["via_uisp_status"] == 1


def test_trapper_only_uisp_host_unknown_or_empty_is_unknown(db_session):
    # "unknown"/unauthorized/empty/other -> UNKNOWN, and NOT counted as a
    # fallback colouring (the trapper supplied no usable colour).
    db_session.add_all([_node("32", "onu-unknown"), _node("33", "onu-empty")])
    db_session.flush()
    hosts = [
        {"hostid": "32", "available": "0", "status": "0", "interfaces": []},
        {"hostid": "33", "available": "0", "status": "0", "interfaces": []},
    ]
    items = [
        {"hostid": "32", "key_": "uisp.status", "lastvalue": "unknown"},
        {"hostid": "33", "key_": "uisp.status", "lastvalue": ""},
    ]

    result = warm_topology_status(db_session, _FakeClient(hosts, [], items))

    by_host = {
        n.zabbix_hostid: n.live_status
        for n in db_session.query(NetworkDevice)
        .filter(NetworkDevice.zabbix_hostid.in_(["32", "33"]))
        .all()
    }
    assert by_host["32"] == "unknown"
    assert by_host["33"] == "unknown"
    assert result["via_uisp_status"] == 0


def test_polled_icmp_wins_over_uisp_trapper(db_session):
    # Host with BOTH icmp=up AND a stale uisp.status="disconnected": polling is
    # authoritative and real-time, so the node stays UP and the trapper does
    # NOT override — nor is it counted as a fallback colouring.
    db_session.add(_node("34", "station-icmp-up-uisp-down"))
    db_session.flush()
    hosts = [{"hostid": "34", "available": "0", "status": "0", "interfaces": []}]
    items = [
        {"hostid": "34", "key_": "icmpping", "lastvalue": "1"},
        {"hostid": "34", "key_": "uisp.status", "lastvalue": "disconnected"},
    ]

    result = warm_topology_status(db_session, _FakeClient(hosts, [], items))

    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="34").one()
    assert n.live_status == "up"
    assert result["via_uisp_status"] == 0


def test_snmp_host_without_uisp_status_unchanged(db_session):
    # An SNMP host with interface available=up and no uisp.status trapper is
    # coloured exactly as before — the trapper arm never touches it.
    db_session.add(_node("35", "snmp-up"))
    db_session.flush()
    hosts = [
        {
            "hostid": "35",
            "available": "0",
            "status": "0",
            "interfaces": [{"main": "1", "available": "1"}],
        }
    ]

    result = warm_topology_status(db_session, _FakeClient(hosts, [], items=[]))

    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="35").one()
    assert n.live_status == "up"
    assert result["via_uisp_status"] == 0


def test_live_session_forces_up_over_polled_down(db_session):
    # Headline false-down guard: polling says DOWN, but the device is carrying a
    # live authenticated customer session -> forced UP, counted once.
    node = _node("40", "serving-bng-polled-down")
    db_session.add(node)
    db_session.flush()
    _seed_live_session(db_session, node)
    hosts = [{"hostid": "40", "available": "2", "status": "0", "interfaces": []}]

    result = warm_topology_status(db_session, _FakeClient(hosts, [], items=[]))

    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="40").one()
    assert n.live_status == "up"
    assert result["via_live_session"] == 1


def test_live_session_forces_up_over_unknown(db_session):
    # UNKNOWN polling + a live session -> UP off the override.
    node = _node("41", "serving-unknown")
    db_session.add(node)
    db_session.flush()
    _seed_live_session(db_session, node)
    hosts = [{"hostid": "41", "available": "0", "status": "0", "interfaces": []}]

    result = warm_topology_status(db_session, _FakeClient(hosts, [], items=[]))

    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="41").one()
    assert n.live_status == "up"
    assert result["via_live_session"] == 1


def test_live_session_on_already_up_not_double_counted(db_session):
    # Polling already UP + a live session -> stays UP, but the override did not
    # change the verdict, so it is NOT counted.
    node = _node("42", "serving-already-up")
    db_session.add(node)
    db_session.flush()
    _seed_live_session(db_session, node)
    hosts = [{"hostid": "42", "available": "1", "status": "0", "interfaces": []}]

    result = warm_topology_status(db_session, _FakeClient(hosts, [], items=[]))

    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="42").one()
    assert n.live_status == "up"
    assert result["via_live_session"] == 0


def test_polled_down_without_live_session_stays_down(db_session):
    # No live session riding it: the arm never fabricates UP — DOWN stays DOWN.
    node = _node("43", "down-no-session")
    db_session.add(node)
    db_session.flush()
    hosts = [{"hostid": "43", "available": "2", "status": "0", "interfaces": []}]

    result = warm_topology_status(db_session, _FakeClient(hosts, [], items=[]))

    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="43").one()
    assert n.live_status == "down"
    assert result["via_live_session"] == 0


def test_live_session_wins_over_uisp_disconnected(db_session):
    # Precedence: a live session AND a uisp.status="disconnected" trapper (which
    # would otherwise colour DOWN) -> UP. Live sessions beat both polling and the
    # uisp arm, and this counts as a live-session override.
    node = _node("44", "serving-uisp-disconnected")
    db_session.add(node)
    db_session.flush()
    _seed_live_session(db_session, node)
    hosts = [{"hostid": "44", "available": "0", "status": "0", "interfaces": []}]
    items = [{"hostid": "44", "key_": "uisp.status", "lastvalue": "disconnected"}]

    result = warm_topology_status(db_session, _FakeClient(hosts, [], items))

    n = db_session.query(NetworkDevice).filter_by(zabbix_hostid="44").one()
    assert n.live_status == "up"
    assert result["via_live_session"] == 1
    # The uisp arm coloured it DOWN first, then the live-session override flipped
    # it UP — so the uisp fallback still fired (counted) but did not win.
    assert result["via_uisp_status"] == 1
