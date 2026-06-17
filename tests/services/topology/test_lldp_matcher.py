"""LLDP neighbor matcher (Phase 2, P2.2)."""

from __future__ import annotations

from app.models.network_monitoring import NetworkDevice
from app.services.topology.lldp_poller import match_neighbor


def _dev(db, name, mgmt_ip=None, hostname=None):
    d = NetworkDevice(name=name, hostname=hostname, mgmt_ip=mgmt_ip, is_active=True)
    db.add(d)
    db.flush()
    return d


def test_identity_matches_normalized_name(db_session):
    d = _dev(db_session, "Gwarimpa Access")
    # router identity often differs in spacing/case
    nb = {"identity": "gwarimpa-access", "interface": "sfp1"}
    assert match_neighbor(db_session, nb) is d


def test_empty_identity_no_ip_is_none(db_session):
    _dev(db_session, "Some Device", mgmt_ip="10.0.0.1")
    assert match_neighbor(db_session, {"identity": "", "interface": "ether1"}) is None


def test_address_fallback_when_no_identity_match(db_session):
    d = _dev(db_session, "Core SW", mgmt_ip="10.10.10.10")
    nb = {"identity": "unknown-box", "address4": "10.10.10.10"}
    assert match_neighbor(db_session, nb) is d


def test_no_identity_and_no_ip_match_is_none(db_session):
    _dev(db_session, "Core SW", mgmt_ip="10.10.10.10")
    nb = {"identity": "mystery", "address4": "192.0.2.250"}
    assert match_neighbor(db_session, nb) is None
