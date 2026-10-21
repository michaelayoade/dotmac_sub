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


@pytest.mark.parametrize("inherit", [False, True])
def test_unknown_history_reactivation_keeps_password_in_both_modes(
    db_session, numbered, catalog_offer, inherit
):
    """NULL keeps the password on the username-collision path in `new` mode too."""
    if inherit:
        _setting(db_session, "pppoe_credential_on_new_service", "inherit")
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _inactive(db_session, numbered, None, "10070000", None)

    _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    assert credential.is_active is True
    assert _plain(credential) == "old-secret"


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


# --------------------- restore-tool cascade and restore (F5, F6) ----------


def _deleted_subscriber(db_session, numbered):
    from app.services import web_system_restore_tool as tool

    numbered.is_active = False
    db_session.commit()
    tool.mark_subscriber_deleted(
        db_session, subscriber_id=str(numbered.id), actor_id=None
    )
    return tool


def test_restore_tool_cascade_records_manual(db_session, numbered, catalog_offer):
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    credential = _cred(db_session, numbered, sub, "10070000")
    inactive = _inactive(db_session, numbered, sub, "other", "service_ended")

    _deleted_subscriber(db_session, numbered)

    db_session.refresh(credential)
    db_session.refresh(inactive)
    assert credential.is_active is False
    assert credential.deactivation_reason == "manual"
    assert inactive.deactivation_reason == "service_ended"


def test_restore_tool_restore_issues_new_password_for_manual_in_new_mode(
    db_session, numbered, catalog_offer
):
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    manual = _inactive(db_session, numbered, sub, "manual-login", "manual")
    ended = _inactive(db_session, numbered, sub, "ended-login", "service_ended")
    tool = _deleted_subscriber(db_session, numbered)

    result = tool.restore_subscriber(
        db_session, subscriber_id=str(numbered.id), actor_id=None
    )

    db_session.refresh(manual)
    db_session.refresh(ended)
    assert result["touched"]["credentials_refused_manual"] == 0
    assert manual.is_active is True
    assert manual.deactivation_reason is None
    assert _plain(manual) != "old-secret"
    assert ended.is_active is True
    assert _plain(ended) == "old-secret"


def test_restore_tool_restore_refuses_manual_in_inherit_mode(
    db_session, numbered, catalog_offer, inherit_on
):
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    manual = _inactive(db_session, numbered, sub, "manual-login", "manual")
    ended = _inactive(db_session, numbered, sub, "ended-login", "service_ended")
    tool = _deleted_subscriber(db_session, numbered)

    result = tool.restore_subscriber(
        db_session, subscriber_id=str(numbered.id), actor_id=None
    )

    db_session.refresh(manual)
    db_session.refresh(ended)
    assert result["touched"]["credentials_refused_manual"] == 1
    assert manual.is_active is False
    assert manual.deactivation_reason == "manual"
    assert _plain(manual) == "old-secret"
    assert ended.is_active is True
    assert _plain(ended) == "old-secret"


# --------------------- bulk activation and backup restore (F3, F4) --------


def test_bulk_activation_reactivation_clears_the_reason(
    db_session, numbered, catalog_offer
):
    from app.services import web_provisioning_bulk_activate as bulk

    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    credential = _inactive(db_session, numbered, sub, "10070000", "manual")

    bulk._upsert_access_credential(
        db_session,
        subscriber=numbered,
        username="10070000",
        password="new-pass",  # noqa: S106
    )
    db_session.commit()

    db_session.refresh(credential)
    assert credential.is_active is True
    assert credential.deactivation_reason is None


def test_backup_restore_that_deactivates_records_manual(
    db_session, numbered, catalog_offer
):
    from app.services.connectivity_backup import (
        capture_connectivity_state,
        restore_connectivity_state,
    )

    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    credential = _inactive(db_session, numbered, sub, "10070000", None)
    backup = capture_connectivity_state(
        db_session, numbered.id, reason="test", include_radius=False
    )
    db_session.commit()
    credential.is_active = True
    db_session.commit()

    restore_connectivity_state(
        db_session,
        backup.id,
        dry_run=False,
        include_radius=False,
        restored_by="tester",
    )

    db_session.refresh(credential)
    assert credential.is_active is False
    assert credential.deactivation_reason == "manual"
