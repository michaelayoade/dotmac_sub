"""Out-of-band notice after MFA removal, for all three principal types."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.notification import Notification
from app.models.subscriber import Reseller, ResellerUser
from app.models.system_user import SystemUser
from app.services.events.handlers.mfa_removal_notice import MfaRemovalNoticeHandler
from app.services.events.types import Event, EventType


def _event(kind, pid, actor, **extra):
    payload = {
        "schema_version": 1,
        "principal_type": kind,
        "principal_id": str(pid),
        "notify_mfa_removal": True,
        "removed_at": datetime(2026, 10, 11, 12, 0, tzinfo=UTC).isoformat(),
        **extra,
    }
    return Event(
        event_type=EventType.credential_administration_completed,
        payload=payload,
        actor=actor,
    )


def _principal(db, kind, subscriber):
    if kind == "subscriber":
        return subscriber.id, subscriber.email
    if kind == "system_user":
        u = SystemUser(
            first_name="T", last_name="U", email=f"{uuid4().hex[:6]}@ex.test"
        )
    else:
        r = Reseller(name=f"R{uuid4().hex[:6]}")
        db.add(r)
        db.flush()
        u = ResellerUser(
            reseller_id=r.id, email=f"{uuid4().hex[:6]}@ex.test", full_name="T"
        )
    db.add(u)
    db.commit()
    return u.id, u.email


@pytest.mark.parametrize("kind", ["subscriber", "system_user", "reseller_user"])
def test_notice_is_queued_to_address_on_file_without_secrets(
    db_session, subscriber, kind
):
    actor = SystemUser(first_name="Ada", last_name="Admin", email="ada@ex.test")
    actor.display_name = "Ada Admin"
    db_session.add(actor)
    db_session.commit()
    pid, email = _principal(db_session, kind, subscriber)
    MfaRemovalNoticeHandler().handle(
        db_session,
        _event(
            kind,
            pid,
            f"user:{actor.id}",
            secret="must-not-leak",
            removal_reason="ticket",
        ),
    )
    rows = db_session.query(Notification).all()
    assert len(rows) == 1
    note = rows[0]
    assert note.recipient == email
    if kind == "system_user":
        assert "Ada Admin" in note.body
    else:
        assert "Ada Admin" not in note.body
        assert "removed by Dotmac support at" in note.body
    assert "2026-10-11 12:00 UTC" in note.body
    assert "contact support or security" in note.body
    assert "must-not-leak" not in note.body + (note.subject or "")
    assert "ticket" not in note.body


def test_other_credential_events_and_unflagged_events_send_nothing(
    db_session, subscriber
):
    handler = MfaRemovalNoticeHandler()
    event = _event("subscriber", subscriber.id, "user:x")
    event.payload["notify_mfa_removal"] = False
    handler.handle(db_session, event)
    handler.handle(
        db_session,
        Event(event_type=EventType.password_recovery_completed, payload={}),
    )
    assert db_session.query(Notification).count() == 0


def test_unknown_actor_falls_back_and_missing_email_is_skipped(db_session, subscriber):
    MfaRemovalNoticeHandler().handle(
        db_session, _event("subscriber", subscriber.id, "api_key:erp")
    )
    assert "by Dotmac support at" in db_session.query(Notification).one().body
    u = SystemUser(first_name="N", last_name="E", email=f"{uuid4().hex[:6]}@ex.test")
    db_session.add(u)
    db_session.commit()
    u.email = ""
    db_session.commit()
    MfaRemovalNoticeHandler().handle(db_session, _event("system_user", u.id, "x"))
    assert db_session.query(Notification).count() == 1


def test_handler_is_registered_with_a_control_policy():
    from app.services import control_relationships as cr

    assert cr.handler_event_types("MfaRemovalNoticeHandler") == frozenset(
        {"credential_administration.completed"}
    )
    assert "MfaRemovalNoticeHandler" in cr.HANDLER_CONTROLS


def test_delivery_failure_cannot_undo_a_committed_removal(db_session, monkeypatch):
    from app.models.auth import MFAMethod, MFAMethodType
    from app.services import staff_provisioning
    from app.services.events.handlers import mfa_removal_notice
    from tests.mfa_removal_helpers import make_admin, make_context, make_staff

    def boom(*_a, **_k):
        raise RuntimeError("mail transport down")

    monkeypatch.setattr(mfa_removal_notice, "submit_communication_intent", boom)
    admin = make_admin(db_session)
    uid = make_staff(db_session, role="viewer")
    db_session.add(
        MFAMethod(
            system_user_id=uid,
            method_type=MFAMethodType.totp,
            label="m",
            secret="s",
            is_primary=True,
            enabled=True,
        )
    )
    db_session.commit()
    outcome = staff_provisioning.remove_staff_mfa(
        db_session,
        staff_provisioning.RemoveStaffMfaCommand(
            context=make_context(admin),
            user_id=uid,
            removal_reason="Verified by call-back, ticket T-3",
        ),
    )
    assert outcome.changed is True
    db_session.expire_all()
    assert db_session.query(MFAMethod).one().secret is None


def _staff_notice(db_session, monkeypatch, *, forced, display_name="Ada Admin"):
    from app.services.events.handlers import mfa_removal_notice as mod

    monkeypatch.setattr(mod, "_force_admin_mfa", lambda _db: forced)
    actor = SystemUser(first_name="A", last_name="B", email="secret-actor@ex.test")
    actor.display_name = display_name
    db_session.add(actor)
    db_session.commit()
    pid, _email = _principal(db_session, "system_user", None)
    MfaRemovalNoticeHandler().handle(
        db_session, _event("system_user", pid, f"user:{actor.id}")
    )
    return db_session.query(Notification).one().body


def test_staff_notice_with_forced_enrolment_on(db_session, monkeypatch):
    body = _staff_notice(db_session, monkeypatch, forced=True)
    assert "asked to set up MFA again at your next sign-in" in body


def test_staff_notice_with_forced_enrolment_off(db_session, monkeypatch):
    body = _staff_notice(db_session, monkeypatch, forced=False)
    assert "next sign-in" not in body
    assert "account security settings" in body


def test_reseller_and_subscriber_notices_never_promise_forced_enrolment(
    db_session, subscriber, monkeypatch
):
    from app.services.events.handlers import mfa_removal_notice as mod

    monkeypatch.setattr(mod, "_force_admin_mfa", lambda _db: True)
    for kind in ("reseller_user", "subscriber"):
        pid, _ = _principal(db_session, kind, subscriber)
        MfaRemovalNoticeHandler().handle(db_session, _event(kind, pid, "user:x"))
    for note in db_session.query(Notification).all():
        assert "next sign-in" not in note.body
        assert "account security settings" in note.body


def test_actor_display_never_falls_back_to_email(db_session, monkeypatch):
    body = _staff_notice(db_session, monkeypatch, forced=False, display_name="")
    assert "secret-actor@ex.test" not in body
    assert "removed by an administrator at" in body
