"""LLDP edge build + canonical dedup + medium (Phase 2, P2.3)."""

from __future__ import annotations

from app.models.network_monitoring import NetworkDevice, TopologyLinkMedium
from app.services.topology.lldp_poller import (
    _build_match_index,
    _canonical,
    accumulate_edges,
)


def _dev(db, name, mgmt_ip=None):
    d = NetworkDevice(name=name, mgmt_ip=mgmt_ip, is_active=True)
    db.add(d)
    db.flush()
    return d


def test_edges_build_drop_cpe_medium_and_dedup(db_session):
    spdc = _dev(db_session, "SPDC Access")
    gbb = _dev(db_session, "GBB")  # matched by identity
    switch = _dev(db_session, "SPDC-Switch", mgmt_ip="10.0.0.77")  # matched by IP
    index = _build_match_index(db_session)

    neighbors = [
        {"identity": "GBB", "interface": "sfp-sfpplus1", "board": "CCR2004"},  # fiber
        {
            "identity": "",
            "interface": "ether5",
            "address4": "192.168.88.50",
        },  # CPE -> drop
        {
            "identity": "x",
            "address4": "10.0.0.77",
            "interface": "ether2",
        },  # switch by IP
        {"identity": "GBB", "interface": "sfp-sfpplus2"},  # duplicate of GBB -> dedup
    ]
    edges = accumulate_edges({}, spdc, neighbors, index)

    assert len(edges) == 2  # GBB + switch; CPE dropped, dup GBB collapsed

    gbb_key = _canonical(spdc.id, gbb.id)
    sw_key = _canonical(spdc.id, switch.id)
    assert gbb_key in edges and sw_key in edges

    # canonical ordering: source <= target by str(uuid)
    for k, e in edges.items():
        assert (e["source_device_id"], e["target_device_id"]) == k
        assert str(e["source_device_id"]) <= str(e["target_device_id"])

    assert edges[gbb_key]["medium"] == TopologyLinkMedium.fiber  # sfp*
    assert edges[sw_key]["medium"] == TopologyLinkMedium.ethernet  # ether*
    # local interface recorded; first GBB observation (sfpplus1) wins
    assert edges[gbb_key]["metadata"]["local_interface"] == "sfp-sfpplus1"
    assert edges[gbb_key]["metadata"]["remote_identity"] == "GBB"


def test_self_link_dropped(db_session):
    spdc = _dev(db_session, "SPDC Access")
    index = _build_match_index(db_session)
    edges = accumulate_edges(
        {}, spdc, [{"identity": "SPDC Access", "interface": "e1"}], index
    )
    assert edges == {}


def test_cross_node_pair_dedups(db_session):
    a = _dev(db_session, "A")
    b = _dev(db_session, "B")
    index = _build_match_index(db_session)
    edges: dict = {}
    accumulate_edges(
        edges, a, [{"identity": "B", "interface": "sfp1"}], index
    )  # A sees B
    accumulate_edges(
        edges, b, [{"identity": "A", "interface": "sfp1"}], index
    )  # B sees A
    assert len(edges) == 1  # one canonical edge
