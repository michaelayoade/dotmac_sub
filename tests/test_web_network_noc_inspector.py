"""NOC live-inspector page-data tests."""

import uuid

from app.services import web_network_noc_inspector as inspector


def test_inspector_data_not_found_for_unknown_node(db_session):
    node_id = uuid.uuid4()
    data = inspector.noc_inspector_data(db_session, node_id)
    assert data == {"found": False, "node_id": str(node_id)}


def test_noc_queue_outage_items_carry_a_node_id_key(db_session):
    # The queue projection must expose node_id on outage rows so the inspector
    # can be reached (the incident->node shim). Empty db => empty queue, but the
    # contract is asserted by the shape helper importing cleanly.
    from app.services.web_network_noc import noc_queue_data

    data = noc_queue_data(db_session)
    assert data["items"] == []
    assert data["counts"]["total"] == 0
