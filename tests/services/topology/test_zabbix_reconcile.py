"""Reconcile match-merge + idempotency (Phase 1, Task 4).

The fixture deliberately SEEDS a pre-existing Splynx-sourced network_device
(no zabbix_hostid) and an existing pop_site that the Zabbix data must MERGE
INTO — a clean-DB fixture would pass even if merge were broken. It also seeds a
device-host + NAS-host sharing one IP to exercise the shared-IP node guard.
"""

from __future__ import annotations

from app.models.catalog import NasDevice
from app.models.network import OLTDevice
from app.models.network_monitoring import NetworkDevice, PopSite
from app.services.topology.zabbix_reconcile import reconcile


class _FakeClient:
    def __init__(self, groups, hosts):
        self._groups = groups
        self._hosts = hosts

    def get_host_groups(self, name=None, limit=1000):
        return self._groups

    def get_hosts(self, **_kw):
        return self._hosts


GROUPS = [
    {"groupid": "10", "name": "Garki BTS"},
    {"groupid": "11", "name": "Lekki BTS"},
    {"groupid": "99", "name": "DotMac/Network/NAS"},
]
HOSTS = [
    {  # OLT device-host — should MERGE into the pre-existing Splynx row by IP
        "hostid": "201",
        "host": "olt2",
        "name": "olt2",
        "interfaces": [{"ip": "10.0.0.8"}],
        "groups": [{"groupid": "10", "name": "Garki BTS"}],
    },
    {  # NAS monitoring host on the SAME IP — duplicate of the node above
        "hostid": "202",
        "host": "NAS: garki",
        "name": "NAS: garki",
        "interfaces": [{"ip": "10.0.0.8"}],
        "groups": [{"groupid": "99", "name": "DotMac/Network/NAS"}],
    },
    {  # new device under Lekki BTS — should CREATE a node, unmatched
        "hostid": "301",
        "host": "lekki-olt",
        "name": "lekki-olt",
        "interfaces": [{"ip": "10.0.1.1"}],
        "groups": [{"groupid": "11", "name": "Lekki BTS"}],
    },
]


def _seed(db_session):
    garki = PopSite(name="Garki", code="GARKI", is_active=True)
    olt = OLTDevice(name="OLT-2", hostname="olt2", mgmt_ip="10.0.0.8", zabbix_host_id="201")
    nas = NasDevice(name="NAS-B", management_ip="10.0.0.8")
    # Orphaned Splynx-sourced node at the OLT's IP — the merge target.
    splynx_node = NetworkDevice(
        name="olt2-splynx",
        hostname="olt2",
        mgmt_ip="10.0.0.8",
        source="splynx",
        splynx_monitoring_id=123,
        is_active=True,
    )
    db_session.add_all([garki, olt, nas, splynx_node])
    db_session.flush()
    return garki, olt, nas, splynx_node


def test_reconcile_merges_and_is_idempotent(db_session):
    garki, olt, nas, splynx_node = _seed(db_session)
    splynx_node_id = splynx_node.id
    client = _FakeClient(GROUPS, HOSTS)

    r1 = reconcile(db_session, client)

    # --- pop_sites: backfilled existing Garki, created Lekki, no duplicate ---
    assert r1["pop_sites"]["backfilled"] == 1
    assert r1["pop_sites"]["created"] == 1
    db_session.refresh(garki)
    assert garki.zabbix_group_id == "10"
    pop_sites = db_session.query(PopSite).all()
    assert len(pop_sites) == 2  # Garki (existing) + Lekki (new); no dup of Garki
    lekki = db_session.query(PopSite).filter_by(zabbix_group_id="11").one()
    assert "Lekki" in lekki.name

    # --- network_devices: merged into the Splynx row (no duplicate) ---
    assert r1["network_devices"]["merged"] == 1
    assert r1["network_devices"]["created"] == 1
    assert r1["network_devices"]["duplicate_host"] == 1  # the NAS sibling
    nodes = db_session.query(NetworkDevice).all()
    assert len(nodes) == 2  # merged Splynx row + new Lekki node; NOT 3

    merged = db_session.query(NetworkDevice).filter_by(id=splynx_node_id).one()
    assert merged.zabbix_hostid == "201"  # backfilled
    assert merged.source == "zabbix_reconcile"
    assert merged.splynx_monitoring_id == 123  # original identity preserved
    assert merged.matched_device_type == "olt"
    assert merged.matched_device_id == olt.id
    assert merged.pop_site_id == garki.id

    # --- idempotency: second run changes nothing but last_synced_at ---
    r2 = reconcile(db_session, client)
    assert r2["pop_sites"]["created"] == 0
    assert r2["pop_sites"]["backfilled"] == 0
    assert r2["pop_sites"]["matched"] == 2
    assert r2["network_devices"]["created"] == 0
    assert r2["network_devices"]["merged"] == 0
    assert r2["network_devices"]["linked"] == 2
    assert r2["network_devices"]["duplicate_host"] == 1

    assert db_session.query(NetworkDevice).count() == 2
    assert db_session.query(PopSite).count() == 2


def test_dry_run_writes_nothing(db_session):
    _seed(db_session)
    client = _FakeClient(GROUPS, HOSTS)
    before_nodes = db_session.query(NetworkDevice).count()
    before_pops = db_session.query(PopSite).count()

    plan = reconcile(db_session, client, dry_run=True)
    db_session.flush()

    assert plan["dry_run"] is True
    # Same decisions surfaced, but no rows added and no backfill written.
    assert plan["pop_sites"]["backfilled"] == 1
    assert plan["network_devices"]["merged"] == 1
    assert db_session.query(NetworkDevice).count() == before_nodes
    assert db_session.query(PopSite).count() == before_pops
    assert (
        db_session.query(PopSite).filter(PopSite.zabbix_group_id.isnot(None)).count()
        == 0
    )
