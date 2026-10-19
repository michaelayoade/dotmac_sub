"""Review follow-ups for the PPPoE credential bridge (H1, M2, M3, M4, L1)."""

# ruff: noqa: F811

from __future__ import annotations

import logging
import sqlite3
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from starlette.datastructures import FormData

from app.models.catalog import AccessCredential, Subscription, SubscriptionStatus
from app.services import radius_population
from app.services import web_catalog_subscription_workflows as workflows
from app.services import web_catalog_subscriptions as web
from app.services.access_credential_secret import (
    AccessCredentialSecretFormat,
    classify_access_credential_secret,
)
from app.services.credential_crypto import decrypt_credential
from app.services.pppoe_credentials import (
    PppoeCredentialError,
    stage_pending_credential,
)
from tests.test_pppoe_credential_bridge import (  # noqa: F401  (fixtures)
    SECRET_PLAIN,
    _cred,
    _edit,
    _ensure,
    _setting,
    _sub,
    inherit_on,
    numbered,
    radius_ok,
    two_services,
)

RESERVED = [
    "env://SOME_VAR",
    "bao://secret/path#field",
    "openbao://secret/path#field",
    "vault://secret/path#field",
    "ENV://SOME_VAR",
    "enc:abcdef",
    "plain:abcdef",
    "cleartext:abcdef",
]
REFS = ["env://SOME_VAR", "bao://secret/path#field", "vault://x#y", "openbao://x#y"]


@pytest.fixture
def no_resolution():
    """Fail the test if anything dereferences a secret reference."""
    from app.services.secrets import is_secret_ref

    def resolver(value, *args, **kwargs):
        if is_secret_ref(value):
            raise AssertionError("secret reference was resolved")
        return value

    with patch("app.services.secrets.resolve_secret", side_effect=resolver) as mock:
        yield mock


def _raw_cred(db, subscriber, sub, username, secret_hash, *, active=True):
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        subscription_id=sub.id if sub is not None else None,
        username=username,
        secret_hash=secret_hash,
        is_active=active,
    )
    db.add(credential)
    db.commit()
    return credential


# ------------------------------- H1: refuse -------------------------------


@pytest.mark.parametrize("typed", RESERVED)
def test_h1_create_refuses_reserved_password(
    db_session, numbered, catalog_offer, typed
):
    before = db_session.query(Subscription).count()
    with pytest.raises(ValueError, match="cannot be a secret reference"):
        web.create_subscription_with_audit(
            db_session,
            {
                "account_id": numbered.id,
                "offer_id": catalog_offer.id,
                "status": "pending",
            },
            FormData([("login", "typed-login"), ("service_password", typed)]),
            None,
            None,
        )
    assert db_session.query(Subscription).count() == before
    assert db_session.query(AccessCredential).count() == 0


@pytest.mark.parametrize("typed", RESERVED)
def test_h1_staging_refuses_reserved_password(
    db_session, numbered, catalog_offer, typed
):
    pending = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    with pytest.raises(ValueError, match="cannot be a secret reference"):
        stage_pending_credential(
            db_session,
            subscriber_id=numbered.id,
            subscription_id=pending.id,
            username="typed-login",
            plain_password=typed,
            radius_profile_id=None,
        )
    assert db_session.query(AccessCredential).count() == 0


@pytest.mark.parametrize("typed", RESERVED)
def test_h1_edit_refuses_reserved_password(db_session, two_services, radius_ok, typed):
    _, b, _, cred_b = two_services
    reconcile, disconnect = radius_ok
    secret_before = cred_b.secret_hash

    with pytest.raises(ValueError, match="cannot be a secret reference"):
        _edit(db_session, b, {"login": "login-b2"}, password=typed)

    db_session.refresh(cred_b)
    assert cred_b.username == "login-b"
    assert cred_b.secret_hash == secret_before
    reconcile.assert_not_called()
    disconnect.assert_not_called()


@pytest.mark.parametrize("typed", REFS)
def test_h1_legacy_upsert_refuses_reference(db_session, subscriber, typed):
    with pytest.raises(ValueError, match="cannot be a secret reference"):
        web._upsert_access_credential(
            db_session,
            subscriber_id=subscriber.id,
            username="legacy-login",
            plain_password=typed,
        )
    assert db_session.query(AccessCredential).count() == 0


# --------------------- H1: stored references are unusable ------------------


@pytest.mark.parametrize("ref", REFS)
def test_h1_classifier_marks_references(ref):
    assert (
        classify_access_credential_secret(ref)
        is AccessCredentialSecretFormat.secret_reference
    )


@pytest.mark.parametrize("ref", REFS)
def test_h1_activation_of_staged_reference_fails_closed(
    db_session, numbered, catalog_offer, no_resolution, ref
):
    pending = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    staged = _raw_cred(db_session, numbered, pending, "typed", ref, active=False)

    with pytest.raises(PppoeCredentialError, match="secret reference"):
        _ensure(db_session, numbered, pending)

    db_session.refresh(staged)
    assert staged.is_active is False
    assert not [
        c
        for c in no_resolution.call_args_list
        if str(c.args[0]).startswith(("env://", "bao://", "vault://", "openbao://"))
    ]


def test_h1_inheriting_a_reference_secret_fails_closed(
    db_session, numbered, catalog_offer, inherit_on, no_resolution
):
    old = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.canceled)
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _raw_cred(db_session, numbered, old, "10070000", "env://X")

    with pytest.raises(PppoeCredentialError, match="secret reference"):
        _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    assert credential.subscription_id == old.id
    assert not [
        c
        for c in no_resolution.call_args_list
        if str(c.args[0]).startswith(("env://", "bao://", "vault://", "openbao://"))
    ]


def test_h1_reactivation_never_keeps_a_reference_secret(
    db_session, numbered, catalog_offer, inherit_on, no_resolution
):
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _raw_cred(
        db_session, numbered, None, "10070000", "env://X", active=False
    )

    _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    assert not credential.secret_hash.startswith("env://")
    assert not [
        c
        for c in no_resolution.call_args_list
        if str(c.args[0]).startswith(("env://", "bao://", "vault://", "openbao://"))
    ]


def test_h1_current_password_is_never_resolved_or_shown(
    db_session, numbered, catalog_offer, no_resolution
):
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    _raw_cred(db_session, numbered, sub, "u1", "env://X")

    assert (
        web._current_service_password(db_session, numbered.id, subscription_id=sub.id)
        is None
    )
    context = web.subscription_form_context(
        db_session,
        {
            "id": str(sub.id),
            "subscriber_id": str(numbered.id),
            "account_id": str(numbered.id),
            "login": "u1",
            "service_password": "",
        },
    )
    assert context["credential_unusable"] is True
    assert context["current_service_password"] == ""
    assert not [
        c
        for c in no_resolution.call_args_list
        if str(c.args[0]).startswith(("env://", "bao://", "vault://", "openbao://"))
    ]


def test_h1_send_credentials_refuses_reference(
    db_session, numbered, catalog_offer, no_resolution
):
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    _raw_cred(db_session, numbered, sub, "u1", "env://X")

    with pytest.raises(ValueError, match="secret reference"):
        web.send_subscription_credentials(db_session, subscription_id=str(sub.id))
    assert not [
        c
        for c in no_resolution.call_args_list
        if str(c.args[0]).startswith(("env://", "bao://", "vault://", "openbao://"))
    ]


def test_h1_reveal_refuses_reference(
    db_session, numbered, catalog_offer, no_resolution
):
    from app.models.subscriber import UserType
    from app.services.web_customer_details import reveal_customer_pppoe_password

    numbered.user_type = UserType.customer
    db_session.commit()
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    _raw_cred(db_session, numbered, sub, "u1", "env://X")

    assert reveal_customer_pppoe_password(db_session, str(numbered.id)) == ("", False)
    assert not [
        c
        for c in no_resolution.call_args_list
        if str(c.args[0]).startswith(("env://", "bao://", "vault://", "openbao://"))
    ]


@pytest.mark.parametrize("ref", REFS)
def test_h1_projection_row_and_dialer_refuse_reference(
    db_session, numbered, catalog_offer, no_resolution, ref
):
    from app.services.cpe_dialer_credential_reconcile import _safe_decrypt
    from app.services.radius import _external_password_row

    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    credential = _raw_cred(db_session, numbered, sub, "u1", ref)

    assert (
        _external_password_row(
            credential, default_attribute="Cleartext-Password", default_op=":="
        )
        is None
    )
    assert _safe_decrypt(ref) is None
    assert not [
        c
        for c in no_resolution.call_args_list
        if str(c.args[0]).startswith(("env://", "bao://", "vault://", "openbao://"))
    ]


def test_h1_population_never_projects_a_reference_password(
    monkeypatch, db_session, numbered, catalog_offer, tmp_path
):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    sub.login = "u1"
    _raw_cred(db_session, numbered, sub, "u1", "env://X")
    target = _sqlite_target(tmp_path)
    _patch_targets(monkeypatch, [target])

    with patch(
        "app.services.secrets.resolve_secret",
        side_effect=AssertionError("resolved"),
    ):
        try:
            result = radius_population.reconcile_usernames(
                {"u1"}, dry_run=False, source_db=db_session
            )
        except radius_population.RadiusProjectionIncomplete:
            result = {"projection_complete": False}

    assert result["projection_complete"] is not True or result.get("unbuildable_logins")
    with sqlite3.connect(target["path"]) as conn:
        values = [row[0] for row in conn.execute("SELECT value FROM radcheck")]
    assert not any("env://" in value for value in values)


# --------------------------------- M2 --------------------------------------


def test_m2_preset_login_matching_ended_predecessor_still_inherits(
    db_session, numbered, catalog_offer, inherit_on
):
    """Bulk activate / API create preset S.login to the old username."""
    old = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.canceled)
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    new.login = "10070000"
    credential = _cred(db_session, numbered, old, "10070000")

    outcome = _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    assert outcome.credential_id == credential.id
    assert credential.subscription_id == new.id


def test_m2_preset_login_matching_ended_predecessor_activates_via_lifecycle_shape(
    db_session, numbered, catalog_offer, inherit_on
):
    old = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.disabled)
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    new.login = "10070000"
    _cred(db_session, numbered, old, "10070000")

    outcome = _ensure(db_session, numbered, new)

    assert outcome.username == "10070000"


def test_m2_restore_refused_only_when_credential_moved_to_live_service(
    db_session, numbered, catalog_offer, inherit_on
):
    old = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    old.login = "10070000"
    live = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.stopped)
    _cred(db_session, numbered, live, "10070000")

    with pytest.raises(PppoeCredentialError, match="replacement service"):
        _ensure(db_session, numbered, old)


# --------------------------------- M3 --------------------------------------


def _legacy_setup(db, numbered, catalog_offer, *, extra_unbound=False):
    own = _sub(db, numbered, catalog_offer, SubscriptionStatus.active)
    own.login = "legacy-login"
    legacy = _cred(db, numbered, None, "legacy-login", secret="legacy-secret")
    if extra_unbound:
        _cred(db, numbered, None, "second-legacy")
    return own, legacy


def test_m3_legacy_edit_needs_an_explicit_choice(
    db_session, numbered, catalog_offer, radius_ok
):
    own, legacy = _legacy_setup(db_session, numbered, catalog_offer)

    with pytest.raises(ValueError, match="legacy login"):
        _edit(db_session, own, {"login": "renamed"})

    db_session.refresh(legacy)
    assert legacy.username == "legacy-login"
    assert legacy.subscription_id is None


def test_m3_legacy_edit_adopts_the_single_unbound_credential(
    db_session, numbered, catalog_offer, radius_ok
):
    own, legacy = _legacy_setup(db_session, numbered, catalog_offer)

    _edit(
        db_session,
        own,
        {"login": "renamed"},
        adopt_legacy_credential_id=str(legacy.id),
    )

    db_session.refresh(legacy)
    assert legacy.subscription_id == own.id
    assert legacy.username == "renamed"
    assert decrypt_credential(legacy.secret_hash) == "legacy-secret"


def test_m3_never_a_guessed_match(db_session, numbered, catalog_offer, radius_ok):
    own, legacy = _legacy_setup(db_session, numbered, catalog_offer, extra_unbound=True)

    with pytest.raises(ValueError, match="no credential of its own"):
        _edit(
            db_session,
            own,
            {"login": "renamed"},
            adopt_legacy_credential_id=str(legacy.id),
        )
    db_session.refresh(legacy)
    assert legacy.subscription_id is None


def test_m3_not_offered_when_another_live_service_exists(
    db_session, numbered, catalog_offer, radius_ok
):
    own, legacy = _legacy_setup(db_session, numbered, catalog_offer)
    _sub(db_session, numbered, catalog_offer, SubscriptionStatus.stopped)

    with pytest.raises(ValueError, match="no credential of its own"):
        _edit(
            db_session,
            own,
            {"login": "renamed"},
            adopt_legacy_credential_id=str(legacy.id),
        )


def test_m3_wrong_choice_id_is_refused(db_session, numbered, catalog_offer, radius_ok):
    own, legacy = _legacy_setup(db_session, numbered, catalog_offer)

    with pytest.raises(ValueError, match="legacy login"):
        _edit(
            db_session,
            own,
            {"login": "renamed"},
            adopt_legacy_credential_id="00000000-0000-0000-0000-000000000000",
        )


def test_m3_form_context_offers_the_choice(db_session, numbered, catalog_offer):
    own, legacy = _legacy_setup(db_session, numbered, catalog_offer)

    context = web.subscription_form_context(
        db_session,
        {
            "id": str(own.id),
            "subscriber_id": str(numbered.id),
            "account_id": str(numbered.id),
            "login": "legacy-login",
            "service_password": "",
        },
    )

    assert context["legacy_credential_candidate"] == {
        "id": str(legacy.id),
        "username": "legacy-login",
    }


def test_m3_handler_passes_the_choice_through(db_session, numbered, catalog_offer):
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    db_session.commit()
    seen = {}

    def fake_update(*args, **kwargs):
        seen.update(kwargs)
        return sub

    with patch.object(web, "update_subscription_with_audit", side_effect=fake_update):
        workflows.handle_subscription_update_form(
            db_session,
            subscription_id=str(sub.id),
            form=FormData(
                {
                    "subscriber_id": str(sub.subscriber_id),
                    "offer_id": str(sub.offer_id),
                    "status": sub.status.value,
                    "billing_mode": sub.billing_mode.value,
                    "contract_term": sub.contract_term.value,
                    "adopt_legacy_credential_id": "abc",
                }
            ),
            request=None,
            actor_id="a",
            credential_update_allowed=True,
        )

    assert seen["adopt_legacy_credential_id"] == "abc"


# --------------------------------- M4 --------------------------------------


def test_m4_unrelated_save_with_login_drift_changes_nothing(
    db_session, two_services, radius_ok
):
    _, b, _, cred_b = two_services
    reconcile, disconnect = radius_ok
    b.login = "drifted-login"
    db_session.commit()

    _, feedback = _edit(
        db_session,
        b,
        {"login": "drifted-login", "service_description": "unrelated"},
    )

    db_session.refresh(cred_b)
    assert cred_b.username == "login-b"
    assert feedback == []
    reconcile.assert_not_called()
    disconnect.assert_not_called()


def test_m4_unrelated_save_by_actor_without_permission_is_allowed(
    db_session, two_services, radius_ok
):
    _, b, _, cred_b = two_services
    reconcile, disconnect = radius_ok
    b.login = "drifted-login"
    db_session.commit()

    _edit(
        db_session,
        b,
        {"login": "drifted-login", "service_description": "unrelated"},
        credential_update_allowed=False,
    )

    db_session.refresh(cred_b)
    assert cred_b.username == "login-b"
    reconcile.assert_not_called()
    disconnect.assert_not_called()


def test_m4_explicit_login_change_still_renames_and_disconnects(
    db_session, two_services, radius_ok
):
    _, b, _, cred_b = two_services
    reconcile, disconnect = radius_ok

    _edit(db_session, b, {"login": "login-b-new"})

    db_session.refresh(cred_b)
    assert cred_b.username == "login-b-new"
    reconcile.assert_called_once()
    disconnect.assert_called_once()


# ------------------------ L1: end to end against RADIUS ---------------------


def _sqlite_target(tmp_path):
    path = tmp_path / "radius.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE radcheck (username TEXT, attribute TEXT, op TEXT, value TEXT)"
        )
        conn.execute(
            "CREATE TABLE radreply (username TEXT, attribute TEXT, op TEXT, value TEXT)"
        )
        conn.execute(
            "CREATE TABLE radusergroup (username TEXT, groupname TEXT, priority INTEGER)"
        )
    return {
        "target_name": "e2e",
        "target_fingerprint": "e2e",
        "db_url": f"sqlite:///{path}",
        "path": str(path),
        "radcheck_table": "radcheck",
        "radreply_table": "radreply",
        "radusergroup_table": "radusergroup",
        "nas_table": "nas",
        "password_attribute": "Cleartext-Password",
        "password_op": ":=",
        "default_reply_op": ":=",
        "use_group": False,
        "group_priority": 0,
    }


def _patch_targets(monkeypatch, targets):
    monkeypatch.setattr(
        radius_population,
        "active_external_radius_targets",
        lambda _db, capability=None: targets,
    )
    monkeypatch.setattr(
        radius_population, "assert_legacy_target_alignment", lambda _db: []
    )
    monkeypatch.setattr(
        radius_population, "simultaneous_use_enforcement_enabled", lambda _db: True
    )


def test_l1_edit_really_removes_the_old_name_from_the_radius_target(
    monkeypatch, db_session, numbered, catalog_offer, tmp_path, caplog
):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    caplog.set_level(logging.DEBUG)
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    sub.login = "e2e-old"
    _cred(db_session, numbered, sub, "e2e-old", secret="old-secret")
    target = _sqlite_target(tmp_path)
    with sqlite3.connect(target["path"]) as conn:
        conn.execute(
            "INSERT INTO radcheck VALUES ('e2e-old','Cleartext-Password',':=','old-secret')"
        )
    _patch_targets(monkeypatch, [target])

    with (
        patch(
            "app.services.enforcement.disconnect_subscription_sessions",
            return_value=1,
        ) as disconnect,
        patch.object(web, "_reconcile_active_subscription_after_credential_sync"),
    ):
        _, feedback = _edit(
            db_session, sub, {"login": "e2e-new"}, password=SECRET_PLAIN
        )

    with sqlite3.connect(target["path"]) as conn:
        rows = conn.execute(
            "SELECT username, attribute, value FROM radcheck"
        ).fetchall()
    usernames = {row[0] for row in rows}
    assert "e2e-old" not in usernames
    assert ("e2e-new", "Cleartext-Password", SECRET_PLAIN) in rows
    assert feedback == []
    assert disconnect.call_args_list[0].kwargs["login"] == "e2e-old"
    assert SECRET_PLAIN not in caplog.text
