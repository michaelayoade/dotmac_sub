"""Known-outage matching + banner + selfcare flag (Phase 4b, P4.5)."""

from __future__ import annotations

from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from app.models.network_monitoring import NetworkDevice, OutageIncident, PopSite
from app.services.topology.customer_path import CustomerPath
from app.services.topology.outage import open_incident_for_path


def _node(db, name):
    n = NetworkDevice(name=name, is_active=True)
    db.add(n)
    db.flush()
    return n


def _open(db, *, root_node_id=None, basestation_id=None):
    inc = OutageIncident(
        root_node_id=root_node_id,
        basestation_id=basestation_id,
        status="open",
        affected_count=7,
        note="fiber cut",
    )
    db.add(inc)
    db.flush()
    return inc


def test_matches_on_node(db_session):
    node = _node(db_session, "n")
    inc = _open(db_session, root_node_id=node.id)
    assert open_incident_for_path(db_session, CustomerPath(node=node)).id == inc.id


def test_matches_on_upstream_hop(db_session):
    node = _node(db_session, "access")
    agg = _node(db_session, "agg")
    inc = _open(db_session, root_node_id=agg.id)
    path = CustomerPath(node=node, upstream_chain=[agg])
    assert open_incident_for_path(db_session, path).id == inc.id


def test_matches_on_basestation(db_session):
    pop = PopSite(name="Garki", zabbix_group_id="10")
    db_session.add(pop)
    db_session.flush()
    inc = _open(db_session, basestation_id=pop.id)
    assert (
        open_incident_for_path(db_session, CustomerPath(basestation=pop)).id == inc.id
    )


def test_resolved_incident_not_matched(db_session):
    node = _node(db_session, "n")
    inc = _open(db_session, root_node_id=node.id)
    inc.status = "resolved"
    db_session.flush()
    assert open_incident_for_path(db_session, CustomerPath(node=node)) is None


def test_none_when_no_incident(db_session):
    node = _node(db_session, "n")
    assert open_incident_for_path(db_session, CustomerPath(node=node)) is None


def test_panel_renders_known_outage_banner():
    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    html = env.get_template("admin/catalog/_network_path_panel.html").render(
        network_path=CustomerPath(
            access_device=SimpleNamespace(name="N"), access_device_kind="nas"
        ),
        known_outage=SimpleNamespace(note="fiber cut", affected_count=7),
    )
    assert "Known outage" in html
    assert "fiber cut" in html
