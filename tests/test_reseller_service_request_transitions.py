"""Transition guard for reseller service-request status (SM-gap #45)."""

import uuid

import pytest
from fastapi import HTTPException

from app.models.subscriber import Reseller
from app.services import reseller_service_requests as svc


def _new_request(db_session) -> str:
    reseller = Reseller(name="SR Trans", code=f"SRT{uuid.uuid4().hex[:8].upper()}")
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)
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


def test_legal_forward_transitions(db_session):
    rid = _new_request(db_session)
    for status in ["reviewing", "scheduled", "completed"]:
        out = svc.update_status(db_session, rid, status=status)
        assert out["status"] == status


def test_terminal_completed_cannot_be_reopened(db_session):
    rid = _new_request(db_session)
    svc.update_status(db_session, rid, status="completed")
    with pytest.raises(HTTPException) as exc:
        svc.update_status(db_session, rid, status="reviewing")
    assert exc.value.status_code == 409


def test_terminal_rejected_cannot_be_flipped_to_completed(db_session):
    rid = _new_request(db_session)
    svc.update_status(db_session, rid, status="rejected")
    with pytest.raises(HTTPException) as exc:
        svc.update_status(db_session, rid, status="completed")
    assert exc.value.status_code == 409


def test_same_status_is_noop_allowed(db_session):
    rid = _new_request(db_session)
    svc.update_status(db_session, rid, status="reviewing")
    out = svc.update_status(db_session, rid, status="reviewing")
    assert out["status"] == "reviewing"


def test_invalid_status_value_still_400(db_session):
    rid = _new_request(db_session)
    with pytest.raises(HTTPException) as exc:
        svc.update_status(db_session, rid, status="bogus")
    assert exc.value.status_code == 400
