from datetime import UTC, datetime

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
)
from app.services.web_network_ont_topology import build_ont_fiber_path


def test_topology_separates_device_operation_from_asset_lifecycle(db_session):
    olt = OLTDevice(name="Topology OLT", is_active=True)
    db_session.add(olt)
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/0", is_active=True)
    ont = OntUnit(
        serial_number="ONT-TOPOLOGY-BINARY",
        olt_device_id=olt.id,
        olt_status=OnuOnlineStatus.online,
        olt_status_seen_at=datetime.now(UTC),
        is_active=True,
    )
    db_session.add_all([pon, ont])
    db_session.flush()
    db_session.add(OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True))
    db_session.commit()

    topology = build_ont_fiber_path(db_session, str(ont.id)).to_dict()
    by_type = {node["node_type"]: node for node in topology["nodes"]}

    assert by_type["ont"]["operational_status"] == "working"
    assert by_type["ont"]["lifecycle_status"] is None
    assert by_type["olt"]["operational_status"] == "not_working"
    assert by_type["pon_port"]["operational_status"] is None
    assert by_type["pon_port"]["lifecycle_status"] == "active"
    assert all("status" not in node for node in topology["nodes"])
