"""Customer portal local-credential management (invite / reset / activate).

Regression for the cross-table bug: these flows used the staff
``web_system_user_mutations`` helpers (``db.get(SystemUser, ...)``), so for a
customer (a Subscriber) they always failed with "User not found". The fix keys
the credential on ``subscriber_id`` (``UserCredential``), the same record the
customer portal accepts at login (local OR RADIUS). Email sending is monkeypatched
so no message is ever dispatched.
"""

import pytest

from app.models.auth import AuthProvider, UserCredential
from app.services import web_customer_user_access as svc


@pytest.fixture
def _no_emails(monkeypatch):
    from app.services import email as email_service

    monkeypatch.setattr(
        email_service, "send_user_invite_email", lambda *a, **k: True, raising=False
    )
    monkeypatch.setattr(
        email_service, "send_password_reset_email", lambda *a, **k: True, raising=False
    )


def _local_cred(db, subscriber_id):
    return (
        db.query(UserCredential)
        .filter(UserCredential.subscriber_id == subscriber_id)
        .filter(UserCredential.provider == AuthProvider.local)
        .first()
    )


def test_activate_login_creates_subscriber_keyed_credential(db_session, subscriber):
    """activate must create a UserCredential keyed on subscriber_id (not SystemUser)."""
    assert _local_cred(db_session, subscriber.id) is None

    svc.activate_customer_login(
        db_session, customer_type="person", customer_id=str(subscriber.id)
    )

    cred = _local_cred(db_session, subscriber.id)
    assert cred is not None
    assert cred.subscriber_id == subscriber.id
    assert cred.system_user_id is None  # not the staff table
    assert cred.is_active is True


def test_deactivate_login_disables_credential(db_session, subscriber):
    svc.activate_customer_login(
        db_session, customer_type="person", customer_id=str(subscriber.id)
    )
    svc.deactivate_customer_login(
        db_session, customer_type="person", customer_id=str(subscriber.id)
    )
    cred = _local_cred(db_session, subscriber.id)
    assert cred is not None
    assert cred.is_active is False


def test_send_invite_succeeds_for_customer(db_session, subscriber, _no_emails):
    """No more 'User not found' — invite ensures the credential and sends."""
    result = svc.send_customer_invite(
        db_session,
        request=None,
        customer_type="person",
        customer_id=str(subscriber.id),
        actor_id=None,
    )
    assert result["ok"] is True, result
    # the local credential the portal will accept now exists
    assert _local_cred(db_session, subscriber.id) is not None


def test_send_reset_link_succeeds_for_customer(db_session, subscriber, _no_emails):
    result = svc.send_customer_reset_link(
        db_session,
        request=None,
        customer_type="person",
        customer_id=str(subscriber.id),
        actor_id=None,
    )
    assert result["ok"] is True, result
