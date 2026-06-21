"""Matcher: Zabbix host -> provisioning device (Phase 1, Task 3)."""

from __future__ import annotations

from app.models.catalog import NasDevice
from app.models.network import OLTDevice
from app.services.topology.zabbix_reconcile import (
    AMBIGUOUS,
    MATCHED,
    UNMATCHED,
    match_host,
)


def _zhost(hostid="100", name="dev", ips=(), groups=()):
    return {
        "hostid": hostid,
        "host": name,
        "name": name,
        "interfaces": [{"ip": ip} for ip in ips],
        "groups": [{"groupid": str(i), "name": g} for i, g in enumerate(groups)],
    }


def test_olt_matched_by_zabbix_host_id(db_session):
    olt = OLTDevice(
        name="OLT-1", hostname="olt1", mgmt_ip="10.0.0.1", zabbix_host_id="555"
    )
    db_session.add(olt)
    db_session.flush()
    t, i, status = match_host(db_session, _zhost(hostid="555", ips=("10.0.0.99",)))
    assert (t, i, status) == ("olt", olt.id, MATCHED)


def test_nas_matched_by_unique_management_ip(db_session):
    nas = NasDevice(name="NAS-A", management_ip="10.0.0.5", nas_ip="172.16.0.5")
    db_session.add(nas)
    db_session.flush()
    z = _zhost(
        hostid="900",
        name="NAS: edge",
        ips=("10.0.0.5",),
        groups=("DotMac/Network/NAS",),
    )
    t, i, status = match_host(db_session, z)
    assert (t, i, status) == ("nas", nas.id, MATCHED)


def test_two_hosts_same_ip_olt_by_hostid_nas_by_name(db_session):
    # An OLT device host and its NAS host can share a management IP. The OLT is
    # disambiguated by its zabbix_host_id (priority 1); the NAS by its "NAS:"
    # name. BTS-group membership must NOT force an OLT classification.
    olt = OLTDevice(
        name="OLT-2", hostname="olt2", mgmt_ip="10.0.0.8", zabbix_host_id="201"
    )
    nas = NasDevice(name="NAS-B", management_ip="10.0.0.8")
    db_session.add_all([olt, nas])
    db_session.flush()

    olt_host = _zhost(
        hostid="201", name="olt2", ips=("10.0.0.8",), groups=("Garki BTS",)
    )
    nas_host = _zhost(
        hostid="202",
        name="NAS: garki",
        ips=("10.0.0.8",),
        groups=("DotMac/Network/NAS",),
    )

    assert match_host(db_session, olt_host) == ("olt", olt.id, MATCHED)
    assert match_host(db_session, nas_host) == ("nas", nas.id, MATCHED)


def test_bts_access_host_matches_nas_not_treated_as_olt(db_session):
    # Regression: a NAS router living in a "*BTS*" group must still match its
    # NasDevice by management IP — BTS membership names the site, not the kind.
    nas = NasDevice(name="Gwarimpa Access", management_ip="160.119.127.81")
    db_session.add(nas)
    db_session.flush()
    z = _zhost(
        hostid="500",
        name="Gwarimpa Access",
        ips=("160.119.127.81",),
        groups=("Gwarimpa BTS",),
    )
    assert match_host(db_session, z) == ("nas", nas.id, MATCHED)


def test_no_candidate_is_unmatched(db_session):
    z = _zhost(
        hostid="777", name="ghost", ips=("192.168.99.99",), groups=("Garki BTS",)
    )
    assert match_host(db_session, z) == (None, None, UNMATCHED)


def test_ambiguous_when_multiple_candidates(db_session):
    # Two NAS share a management IP (NasDevice.management_ip is not unique) and
    # the host can't be narrowed further -> ambiguous, never pick first.
    db_session.add_all(
        [
            NasDevice(name="NAS-X", management_ip="10.9.9.9"),
            NasDevice(name="NAS-Y", management_ip="10.9.9.9"),
        ]
    )
    db_session.flush()
    z = _zhost(
        hostid="303", name="NAS: dup", ips=("10.9.9.9",), groups=("DotMac/Network/NAS",)
    )
    assert match_host(db_session, z) == (None, None, AMBIGUOUS)
