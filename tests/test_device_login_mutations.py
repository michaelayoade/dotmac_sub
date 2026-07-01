"""Tests for device-login mutations in web_system_user_mutations."""

from __future__ import annotations

import uuid

import pytest

from app.models.system_user import SystemUser
from app.services.credential_crypto import decrypt_credential
from app.services.web_system_user_mutations import revoke_device_login, set_device_login


@pytest.fixture()
def system_user(db_session):
    """Minimal active SystemUser for device-login mutation tests."""
    user = SystemUser(
        id=uuid.uuid4(),
        first_name="Device",
        last_name="Login",
        email=f"device-login-{uuid.uuid4().hex}@example.com",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_set_device_login_encrypts_secret(db_session, system_user):
    u = set_device_login(
        db_session, user_id=str(system_user.id), enabled=True, secret="P@ss"
    )
    assert u.device_login_enabled is True
    assert u.device_login_secret and u.device_login_secret != "P@ss"
    assert decrypt_credential(u.device_login_secret) == "P@ss"
    assert u.device_login_secret_set_at is not None


def test_revoke_sets_timestamp_and_disables(db_session, system_user):
    set_device_login(
        db_session, user_id=str(system_user.id), enabled=True, secret="P@ss"
    )
    u = revoke_device_login(db_session, user_id=str(system_user.id))
    assert u.device_login_enabled is False
    assert u.device_login_revoked_at is not None


def test_reenable_without_secret_clears_revoked(db_session, system_user):
    """Re-enabling without supplying a new secret must clear device_login_revoked_at."""
    # Step 1: enable with a secret
    set_device_login(
        db_session, user_id=str(system_user.id), enabled=True, secret="P@ss"
    )
    # Step 2: revoke (sets device_login_revoked_at)
    revoke_device_login(db_session, user_id=str(system_user.id))
    # Step 3: re-enable without a new secret
    u = set_device_login(
        db_session, user_id=str(system_user.id), enabled=True, secret=None
    )
    # Both conditions must hold
    assert u.device_login_enabled is True
    assert u.device_login_revoked_at is None


def test_set_device_login_commit_false_does_not_commit(
    db_session, system_user, monkeypatch
):
    """commit=False flushes but does not commit, so the caller (route) can commit
    the credential change and its audit row atomically. It still applies the
    change in-session for the caller to persist.
    """
    import unittest.mock as mock

    spy = mock.Mock(wraps=db_session.commit)
    monkeypatch.setattr(db_session, "commit", spy)

    set_device_login(
        db_session,
        user_id=str(system_user.id),
        enabled=True,
        secret="P@ss",
        commit=False,
    )
    spy.assert_not_called()
    assert system_user.device_login_enabled is True  # applied, pending caller commit

    # Default (commit=True) still commits — backward compatible.
    set_device_login(
        db_session, user_id=str(system_user.id), enabled=False, secret=None
    )
    spy.assert_called()
