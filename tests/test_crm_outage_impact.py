from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.api import crm as crm_routes
from app.models.network_monitoring import NetworkDevice
from app.services import crm_api


def _olt_node(db_session, *, matched: bool = True) -> NetworkDevice:
    node = NetworkDevice(
        name="OLT-Ikeja-1",
        matched_device_type="olt" if matched else None,
        matched_device_id=uuid.uuid4() if matched else None,
        is_active=True,
    )
    db_session.add(node)
    db_session.commit()
    db_session.refresh(node)
    return node


def test_impact_requires_a_parameter(db_session):
    with pytest.raises(HTTPException) as exc:
        crm_routes.outage_impact(node_id=None, basestation_id=None, db=db_session)
    assert exc.value.status_code == 400


def test_impact_node_not_found(db_session):
    with pytest.raises(HTTPException) as exc:
        crm_routes.outage_impact(
            node_id=str(uuid.uuid4()), basestation_id=None, db=db_session
        )
    assert exc.value.status_code == 404


def test_impact_flags_topology_gap_when_olt_has_no_subscribers(db_session):
    """A matched OLT node that resolves no subscribers is surfaced as a gap —
    the e2e ONT/assignment chain isn't established, so impact is incomplete."""
    node = _olt_node(db_session)
    report = crm_api.outage_impact(db_session, node=node)
    assert report["subscribers"] == []
    assert report["count"] == 0
    assert report["coverage"]["has_topology_gaps"] is True
    gap_ids = {g["node_id"] for g in report["coverage"]["nodes_without_subscribers"]}
    assert str(node.id) in gap_ids


def test_impact_route_returns_envelope(db_session):
    node = _olt_node(db_session)
    resp = crm_routes.outage_impact(
        node_id=str(node.id), basestation_id=None, db=db_session
    )
    assert "data" in resp
    assert resp["data"]["coverage"]["resolved_node_count"] >= 1


def _chain(db_session, *, vendor="huawei"):
    """Minimal OLT → PON port → ONT → active assignment → subscriber chain."""
    from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
    from app.models.subscriber import Subscriber

    olt = OLTDevice(name="OLT-Ikeja", vendor=vendor)
    db_session.add(olt)
    db_session.commit()
    port = PonPort(olt_id=olt.id, name="0/1/2")
    ont = OntUnit(olt_device_id=olt.id, serial_number=uuid.uuid4().hex[:12])
    sub = Subscriber(
        first_name="Ada",
        last_name="L",
        email=f"a-{uuid.uuid4().hex[:8]}@x.io",
        subscriber_number=f"SUB-{uuid.uuid4().hex[:6]}",
    )
    db_session.add_all([port, ont, sub])
    db_session.commit()
    assign = OntAssignment(
        ont_unit_id=ont.id, pon_port_id=port.id, subscriber_id=sub.id, active=True
    )
    db_session.add(assign)
    db_session.commit()
    return {"olt": olt, "port": port, "ont": ont, "sub": sub}


def test_impact_by_pon_port_returns_only_that_ports_customers(db_session):
    c = _chain(db_session)
    report = crm_api.outage_impact(db_session, pon_port_id=c["port"].id)
    assert report["count"] == 1
    assert report["subscribers"][0]["subscriber_number"] == c["sub"].subscriber_number
    assert report["coverage"]["has_topology_gaps"] is False


def test_impact_by_olt_returns_all_its_customers(db_session):
    c = _chain(db_session, vendor="ubiquiti")
    report = crm_api.outage_impact(db_session, olt_id=c["olt"].id)
    assert report["count"] == 1
    assert report["subscribers"][0]["id"] == str(c["sub"].id)


def test_impact_pon_port_gap_when_no_assignments(db_session):
    from app.models.network import OLTDevice, PonPort

    olt = OLTDevice(name="OLT-empty", vendor="huawei")
    db_session.add(olt)
    db_session.commit()
    port = PonPort(olt_id=olt.id, name="0/0/0")
    db_session.add(port)
    db_session.commit()
    report = crm_api.outage_impact(db_session, pon_port_id=port.id)
    assert report["count"] == 0
    assert report["coverage"]["has_topology_gaps"] is True


def test_list_infrastructure_assets_includes_olt_and_pon_port(db_session):
    c = _chain(db_session, vendor="huawei")
    assets = crm_api.list_infrastructure_assets(db_session)
    by_type = {a["type"] for a in assets}
    assert "olt" in by_type
    assert "pon_port" in by_type
    olt_asset = next(
        a for a in assets if a["type"] == "olt" and a["id"] == str(c["olt"].id)
    )
    assert "huawei" in olt_asset["label"].lower()
    port_asset = next(
        a for a in assets if a["type"] == "pon_port" and a["id"] == str(c["port"].id)
    )
    assert c["port"].name in port_asset["label"]
