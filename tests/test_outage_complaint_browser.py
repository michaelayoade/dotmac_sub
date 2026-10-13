"""Complaint adapter CSRF and stale-form transport contract."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.domain_errors import DomainError
from app.services.permission_admission import PermissionAdmission
from app.web.admin import outage_complaints as web


def _access():
    return PermissionAdmission(uuid4(), "system_user", frozenset())


def test_post_refuses_invalid_csrf_before_command(monkeypatch):
    monkeypatch.setattr(
        web, "parse_form_data_sync", lambda request: {"_csrf_token": "wrong"}
    )
    request = SimpleNamespace(cookies={web.CSRF_COOKIE_NAME: "expected"})
    access = _access()
    with pytest.raises(HTTPException) as caught:
        web.complaint_link_submit(
            request,
            uuid4(),
            db=None,
            support=access,
            monitoring=access,
            support_read=access,
            monitoring_read=access,
        )
    assert caught.value.status_code == 403


def test_stale_post_preserves_original_evidence_and_intent(monkeypatch):
    submitted = {
        "_csrf_token": "match",
        "incident_id": str(uuid4()),
        "subscription_id": str(uuid4()),
        "revision_id": str(uuid4()),
        "membership_token": "a" * 64,
        "reason": "Reviewed before reroot",
        "intent_key": str(uuid4()),
    }
    monkeypatch.setattr(web, "parse_form_data_sync", lambda request: submitted)
    monkeypatch.setattr(
        web.db_session_adapter, "release_read_transaction", lambda db: None
    )
    captured = {}

    def conflict(db, *, command):
        captured["command"] = command
        raise DomainError(
            code="outage.complaint.stale_revision", message="Reload current evidence"
        )

    monkeypatch.setattr(web.outage, "link_complaint_ticket", conflict)
    monkeypatch.setattr(web, "_render", lambda request, db, **kwargs: kwargs)
    access = _access()
    result = web.complaint_link_submit(
        SimpleNamespace(cookies={web.CSRF_COOKIE_NAME: "match"}),
        uuid4(),
        db=None,
        support=access,
        monitoring=access,
        support_read=access,
        monitoring_read=access,
    )
    assert result["status"] == 409
    assert result["submitted"]["revision_id"] == submitted["revision_id"]
    assert result["submitted"]["intent_key"] == submitted["intent_key"]
    assert str(captured["command"].expected_revision_id) == submitted["revision_id"]
    assert (
        captured["command"].expected_membership_token == submitted["membership_token"]
    )
