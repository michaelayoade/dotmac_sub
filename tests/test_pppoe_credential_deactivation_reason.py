"""M1 gate: keep the password on restore only after a service-ended deactivation."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.catalog import AccessCredential, SubscriptionStatus
from app.schemas.catalog import AccessCredentialUpdate
from app.services import catalog as catalog_service
from app.services.credential_crypto import decrypt_credential
from app.services.pppoe_credentials import PppoeCredentialError
from tests.test_pppoe_credential_bridge import (  # noqa: F401
    _cred,
    _ensure,
    _setting,
    _sub,
    inherit_on,
    numbered,
)

# ruff: noqa: F811


def _inactive(db, subscriber, sub, username, reason, secret="old-secret"):  # noqa: S107
    credential = _cred(db, subscriber, sub, username, active=False, secret=secret)
    credential.deactivation_reason = reason
    db.commit()
    return credential


def _plain(credential):
    return decrypt_credential(credential.secret_hash)


# ------------------------------- writers -----------------------------------


def test_cancel_cleanup_records_service_ended(db_session, numbered, catalog_offer):
    from app.services.enforcement import cleanup_subscription_on_cancel

    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.canceled)
    credential = _cred(db_session, numbered, sub, "10070000")

    with (
        patch("app.services.enforcement._remove_credentials_from_external_radius"),
        patch(
            "app.services.enforcement.disconnect_subscription_sessions",
            return_value=0,
        ),
        patch("app.services.connectivity_backup.capture_connectivity_state"),
    ):
        stats = cleanup_subscription_on_cancel(db_session, str(sub.id))

    db_session.refresh(credential)
    assert stats["credentials_deactivated"] == 1
    assert credential.is_active is False
    assert credential.deactivation_reason == "service_ended"


def test_rest_update_and_delete_record_manual_and_reactivation_clears(
    db_session, numbered, catalog_offer
):
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    credential = _cred(db_session, numbered, sub, "10070000")

    with patch("app.services.catalog.credentials._sync_credential_to_radius"):
        catalog_service.access_credentials.update(
            db_session, str(credential.id), AccessCredentialUpdate(is_active=False)
        )
        db_session.refresh(credential)
        assert credential.deactivation_reason == "manual"

        catalog_service.access_credentials.update(
            db_session, str(credential.id), AccessCredentialUpdate(is_active=True)
        )
        db_session.refresh(credential)
        assert credential.is_active is True
        assert credential.deactivation_reason is None

        catalog_service.access_credentials.delete(db_session, str(credential.id))
    db_session.refresh(credential)
    assert credential.is_active is False
    assert credential.deactivation_reason == "manual"


# --------------------- restore of the same service (staged branch) ---------


@pytest.mark.parametrize("inherit", [False, True])
def test_service_ended_restore_keeps_the_password(
    db_session, numbered, catalog_offer, inherit
):
    if inherit:
        _setting(db_session, "pppoe_credential_on_new_service", "inherit")
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    credential = _inactive(db_session, numbered, sub, "10070000", "service_ended")

    outcome = _ensure(db_session, numbered, sub)

    db_session.refresh(credential)
    assert outcome.credential_id == credential.id
    assert credential.is_active is True
    assert _plain(credential) == "old-secret"
    assert credential.deactivation_reason is None


@pytest.mark.parametrize("inherit", [False, True])
def test_unknown_history_restore_keeps_the_password(
    db_session, numbered, catalog_offer, inherit
):
    if inherit:
        _setting(db_session, "pppoe_credential_on_new_service", "inherit")
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    credential = _inactive(db_session, numbered, sub, "10070000", None)

    outcome = _ensure(db_session, numbered, sub)

    db_session.refresh(credential)
    assert outcome.credential_id == credential.id
    assert _plain(credential) == "old-secret"


def test_manual_deactivation_with_new_setting_issues_a_new_credential(
    db_session, numbered, catalog_offer
):
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    credential = _inactive(db_session, numbered, sub, "custom-login", "manual")

    outcome = _ensure(db_session, numbered, sub)

    db_session.refresh(credential)
    assert outcome.credential_id != credential.id
    assert credential.is_active is False
    assert credential.deactivation_reason == "manual"
    assert _plain(credential) == "old-secret"
    fresh = db_session.get(AccessCredential, outcome.credential_id)
    assert _plain(fresh) != "old-secret"


def test_manual_deactivation_with_inherit_refuses(
    db_session, numbered, catalog_offer, inherit_on
):
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    credential = _inactive(db_session, numbered, sub, "custom-login", "manual")

    with pytest.raises(PppoeCredentialError, match="deactivated manually"):
        _ensure(db_session, numbered, sub)

    db_session.refresh(credential)
    assert credential.is_active is False
    assert _plain(credential) == "old-secret"


# --------------------- username-collision reactivation path ----------------


def test_service_ended_reactivation_keeps_password_even_in_new_mode(
    db_session, numbered, catalog_offer
):
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _inactive(db_session, numbered, None, "10070000", "service_ended")

    _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    assert credential.is_active is True
    assert _plain(credential) == "old-secret"
    assert credential.deactivation_reason is None


def test_manual_reactivation_overwrites_in_new_mode(
    db_session, numbered, catalog_offer
):
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _inactive(db_session, numbered, None, "10070000", "manual")

    _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    assert _plain(credential) != "old-secret"
    assert credential.deactivation_reason is None


def test_manual_reactivation_refuses_in_inherit_mode(
    db_session, numbered, catalog_offer, inherit_on
):
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _inactive(db_session, numbered, None, "10070000", "manual")

    with pytest.raises(PppoeCredentialError, match="deactivated manually"):
        _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    assert credential.is_active is False


def test_unknown_history_reactivation_is_unchanged(db_session, numbered, catalog_offer):
    """(G) NULL keeps today's behaviour per mode: `new` overwrites here."""
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _inactive(db_session, numbered, None, "10070000", None)

    _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    assert _plain(credential) != "old-secret"


# ------------------------------- inherit -----------------------------------


def test_inherit_refuses_a_manually_deactivated_predecessor_credential(
    db_session, numbered, catalog_offer, inherit_on
):
    old = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.canceled)
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _inactive(db_session, numbered, old, "legacy-name", "manual")

    with pytest.raises(PppoeCredentialError, match="deactivated manually"):
        _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    assert credential.subscription_id == old.id


def test_inherit_reuses_a_service_ended_predecessor_credential(
    db_session, numbered, catalog_offer, inherit_on
):
    old = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.canceled)
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _inactive(db_session, numbered, old, "legacy-name", "service_ended")

    outcome = _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    assert outcome.credential_id == credential.id
    assert _plain(credential) == "old-secret"
    assert credential.deactivation_reason is None
