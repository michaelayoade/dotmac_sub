"""In-app account deletion = soft-delete (status canceled), App Store 5.1.1(v)."""

import uuid

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.subscriber import Subscriber, SubscriberStatus, UserType
from app.services import account_deletion


def _subscriber(db):
    s = Subscriber(
        first_name="Del",
        last_name="User",
        email="del.user@example.com",
        user_type=UserType.customer,
        status=SubscriberStatus.active,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_deletion_soft_deletes_audits_and_is_idempotent(db_session):
    s = _subscriber(db_session)

    out = account_deletion.request_deletion(db_session, str(s.id), reason="moving")
    assert out["status"] == "deleted"
    assert out["already_requested"] is False

    db_session.refresh(s)
    # Soft-delete: record preserved, status canceled (blocks login), deactivated.
    assert s.status == SubscriberStatus.canceled
    assert s.is_active is False
    meta = s.metadata_ or {}
    assert meta.get("account_deletion_requested_at")
    assert meta.get("account_deletion_reason") == "moving"

    events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "account_deletion_requested")
        .all()
    )
    assert len(events) == 1
    assert events[0].entity_id == str(s.id)

    # Idempotent: re-requesting an already-canceled account is a no-op success.
    out2 = account_deletion.request_deletion(db_session, str(s.id))
    assert out2["already_requested"] is True
    db_session.refresh(s)
    assert s.status == SubscriberStatus.canceled


def test_deletion_unknown_subscriber_404(db_session):
    with pytest.raises(HTTPException) as exc:
        account_deletion.request_deletion(db_session, str(uuid.uuid4()))
    assert exc.value.status_code == 404
