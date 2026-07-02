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
