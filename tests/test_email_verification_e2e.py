"""End-to-end coverage for the customer email-verification loop.

Exercises the full chain at the service layer: dispatch a verification token,
verify it (flipping ``Subscriber.email_verified``), idempotency, bad-token
rejection, the already-verified no-op, and the profile email-change path that
re-arms verification (reset flag + re-dispatch).
"""

import uuid

import pytest

from app.services import auth_flow as auth_flow_service
from app.services import customer_portal_profile_commands as profile_commands
from app.services import email_verification
from app.services.audit_adapter import AuditActor
from app.services.owner_commands import CommandContext
from tests.customer_profile_owner_support import (
    profile_command_context,
    profile_owner_session,
)


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")


@pytest.fixture()
def _no_audit(monkeypatch):
    """Audit sinks aren't the unit under test — make them inert."""
    import app.services.audit_adapter as audit_adapter

    monkeypatch.setattr(audit_adapter, "record_audit_event", lambda *a, **k: None)


@pytest.fixture()
def _capture_email(monkeypatch):
    """Replace the SMTP sender with a capture so no mail is actually sent."""
    sent: list[dict] = []

    def _fake_send(
        *, db, to_email, verification_token, person_name=None, expires_minutes=None
    ):
        sent.append({"to_email": to_email, "token": verification_token})
        return True

    import app.services.email as email_service

    monkeypatch.setattr(email_service, "send_email_verification_email", _fake_send)
    return sent


def _complete(caller, token):
    with profile_owner_session(caller) as owner_db:
        return email_verification.complete_email_verification(
            owner_db,
            command=email_verification.CompleteEmailVerificationCommand(
                context=CommandContext.system(
                    actor="capability:email-verification",
                    scope=email_verification.EMAIL_VERIFICATION_SCOPE,
                    reason="Email completion fast-unit test",
                ),
                token=token,
            ),
        )


def test_verify_email_round_trip(db_session, subscriber, _no_audit):
    """A freshly minted token flips the subscriber to verified, idempotently."""
    assert subscriber.email_verified is False

    token = auth_flow_service._issue_email_verification_token(
        db_session, str(subscriber.id), subscriber.email
    )
    result = _complete(db_session, token)
    assert result.email_verified is True

    db_session.refresh(subscriber)
    assert subscriber.email_verified is True

    # Idempotent: verifying again is a success no-op, not an error.
    again = _complete(db_session, token)
    assert again.email_verified is True


def test_verify_email_rejects_bad_token(db_session, _no_audit):
    with pytest.raises(email_verification.EmailVerificationError):
        _complete(db_session, "not-a-real-token")


def test_verify_email_rejects_wrong_email(db_session, subscriber, _no_audit):
    """A token whose email no longer matches the subscriber is rejected."""
    token = auth_flow_service._issue_email_verification_token(
        db_session, str(subscriber.id), "stale@example.com"
    )
    with pytest.raises(email_verification.EmailVerificationError):
        _complete(db_session, token)
    db_session.refresh(subscriber)
    assert subscriber.email_verified is False


def test_send_dispatches_when_unverified(
    db_session, subscriber, _no_audit, _capture_email
):
    sent = auth_flow_service.send_email_verification(db_session, str(subscriber.id))
    assert sent is True
    assert len(_capture_email) == 1
    assert _capture_email[0]["to_email"] == subscriber.email


def test_send_skips_when_already_verified(
    db_session, subscriber, _no_audit, _capture_email
):
    subscriber.email_verified = True
    db_session.commit()
    sent = auth_flow_service.send_email_verification(db_session, str(subscriber.id))
    assert sent is False
    assert _capture_email == []


def test_send_noop_for_missing_subscriber(db_session, _no_audit, _capture_email):
    sent = auth_flow_service.send_email_verification(db_session, str(uuid.uuid4()))
    assert sent is False
    assert _capture_email == []


def test_profile_email_change_rearms_verification(
    db_session, subscriber, _no_audit, _capture_email
):
    """Changing the email resets verified=False AND re-dispatches a link."""
    subscriber.email_verified = True
    db_session.commit()

    new_email = f"changed-{uuid.uuid4().hex[:8]}@example.com"
    with profile_owner_session(db_session) as owner_db:
        outcome = profile_commands.update_customer_profile(
            owner_db,
            command=profile_commands.UpdateCustomerProfileCommand(
                context=profile_command_context(actor_id=subscriber.id),
                actor=AuditActor.user(str(subscriber.id)),
                subscriber_id=subscriber.id,
                first_name="Test",
                last_name="User",
                email=new_email,
                phone=None,
                billing_notifications=False,
                sms_updates=False,
            ),
        )
    db_session.refresh(subscriber)
    assert outcome is not None
    updated = subscriber
    assert updated.email == new_email
    assert updated.email_verified is False

    # The profile transaction stages a delivery; it does not call SMTP.
    from sqlalchemy import select

    from app.models.notification import Notification

    assert _capture_email == []
    notification = db_session.scalars(
        select(Notification).where(Notification.event_type == "auth.email_verification")
    ).one()
    assert notification.recipient == new_email
    assert notification.body is None


def test_profile_same_email_does_not_resend(
    db_session, subscriber, _no_audit, _capture_email
):
    """No email change → verified state preserved, no new mail."""
    subscriber.email_verified = True
    db_session.commit()

    with profile_owner_session(db_session) as owner_db:
        outcome = profile_commands.update_customer_profile(
            owner_db,
            command=profile_commands.UpdateCustomerProfileCommand(
                context=profile_command_context(actor_id=subscriber.id),
                actor=AuditActor.user(str(subscriber.id)),
                subscriber_id=subscriber.id,
                first_name="Test",
                last_name="User",
                email=subscriber.email,
                phone=None,
                billing_notifications=False,
                sms_updates=False,
            ),
        )
    db_session.refresh(subscriber)
    assert outcome is not None
    updated = subscriber
    assert updated.email_verified is True
    assert _capture_email == []


def test_set_subscriber_email_adds_and_sends(
    db_session, subscriber, _no_audit, _capture_email
):
    """Shared helper: changing/adding an email re-arms verification + sends."""
    subscriber.email_verified = True
    db_session.commit()

    new_email = f"added-{uuid.uuid4().hex[:8]}@example.com"
    changed = auth_flow_service.set_subscriber_email(
        db_session, str(subscriber.id), new_email
    )
    assert changed is True
    db_session.refresh(subscriber)
    assert subscriber.email == new_email
    assert subscriber.email_verified is False
    assert _capture_email and _capture_email[-1]["to_email"] == new_email


def test_set_subscriber_email_unchanged_is_noop(
    db_session, subscriber, _no_audit, _capture_email
):
    changed = auth_flow_service.set_subscriber_email(
        db_session, str(subscriber.id), subscriber.email
    )
    assert changed is False
    assert _capture_email == []


def test_set_subscriber_email_allows_duplicate(
    db_session, subscriber, _no_audit, _capture_email
):
    """Email is non-unique contact info — sharing another subscriber's address
    is allowed (no 409) and re-arms verification."""
    from app.models.subscriber import Subscriber

    other = Subscriber(
        first_name="Other",
        last_name="Person",
        email=f"taken-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add(other)
    db_session.commit()

    changed = auth_flow_service.set_subscriber_email(
        db_session, str(subscriber.id), other.email
    )
    assert changed is True
    db_session.refresh(subscriber)
    assert subscriber.email == other.email


def test_me_update_can_add_email_and_verify(
    db_session, subscriber, _no_audit, _capture_email
):
    """The mobile /me update path can add/change email and re-arm verification."""
    from app.schemas.auth_flow import MeUpdateRequest
    from app.services import user_profile

    subscriber.email_verified = True
    db_session.commit()

    new_email = f"viaapp-{uuid.uuid4().hex[:8]}@example.com"
    user_profile.update_me(
        db_session,
        principal_id=subscriber.id,
        principal_type="subscriber",
        payload=MeUpdateRequest(email=new_email),
        roles=[],
        scopes=[],
    )
    db_session.refresh(subscriber)
    assert subscriber.email == new_email
    assert subscriber.email_verified is False
    assert _capture_email and _capture_email[-1]["to_email"] == new_email
