"""Tests for the admin reseller service-request queue web service."""

import uuid

from app.models.subscriber import Reseller
from app.services import reseller_service_requests as svc
from app.services import web_service_requests


def _reseller(db_session):
    r = Reseller(name="SR Web", code=f"SRW{uuid.uuid4().hex[:8].upper()}")
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


def _new_request(db_session, reseller):
    created = svc.create_request(
        db_session,
        str(reseller.id),
        subscriber_id=None,
        contact_name="Lead Person",
        contact_phone="08012345678",
        contact_email="lead@example.com",
        address="1 Test Road",
        latitude=None,
        longitude=None,
        notes=None,
    )
    return created["id"]


def test_allowed_next_statuses_new_and_terminal():
    nexts = {s.value for s in svc.allowed_next_statuses("new")}
    assert nexts == {"reviewing", "scheduled", "completed", "rejected"}
    assert svc.allowed_next_statuses("completed") == []
    assert svc.allowed_next_statuses("rejected") == []


def test_list_data_counts_and_filter(db_session):
    r = _reseller(db_session)
    id1 = _new_request(db_session, r)
    id2 = _new_request(db_session, r)
    svc.update_status(db_session, id2, status="reviewing")

    data = web_service_requests.list_data(db_session, status=None, page=1, per_page=25)
    assert data["total"] >= 2
    assert data["new_count"] >= 1
    assert "reviewing" in data["statuses"]

    only_new = web_service_requests.list_data(
        db_session, status="new", page=1, per_page=25
    )
    ids = {str(x.id) for x in only_new["requests"]}
    assert id1 in ids
    assert id2 not in ids

    # An invalid status is ignored and the filter is cleared (shows all).
    bad = web_service_requests.list_data(
        db_session, status="bogus", page=1, per_page=25
    )
    assert bad["status_filter"] is None


def test_detail_data_shape_and_missing(db_session):
    r = _reseller(db_session)
    rid = _new_request(db_session, r)

    detail = web_service_requests.detail_data(db_session, request_id=rid)
    assert detail is not None
    assert str(detail["req"].id) == rid
    assert detail["reseller"].id == r.id
    assert set(detail["allowed_next"]) == {
        "reviewing",
        "scheduled",
        "completed",
        "rejected",
    }

    assert (
        web_service_requests.detail_data(db_session, request_id=str(uuid.uuid4()))
        is None
    )
