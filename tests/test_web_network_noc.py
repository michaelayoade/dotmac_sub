"""The NOC queue merges the open outage / mismatch / alarm owners into one list."""

from app.services.web_network_noc import noc_queue_data


def test_noc_queue_shape_and_empty(db_session):
    data = noc_queue_data(db_session)
    assert set(data["counts"]) == {"total", "outages", "mismatches", "alarms"}
    assert data["counts"]["total"] == len(data["items"])
    # empty test DB → nothing in queue, but all three owner reads must run cleanly
    assert data["items"] == []
    assert data["counts"]["total"] == 0
