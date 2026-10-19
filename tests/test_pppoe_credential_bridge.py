"""PPPoE credential bridge (Option C): inherit-by-setting and exact edit path.

Every regression test here fails on the pre-bridge code; tests marked (G) are
guards that pass before and after.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from starlette.datastructures import FormData

from app.models.catalog import AccessCredential, Subscription, SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.event_store import EventStore
from app.models.subscription_engine import SettingValueType
from app.services import auth_flow as auth_flow_service
from app.services import settings_spec
from app.services import web_catalog_subscription_workflows as workflows
from app.services import web_catalog_subscriptions as web
from app.services.credential_crypto import decrypt_credential, encrypt_credential
from app.services.pppoe_credentials import (
    EnsurePppoeCredentialCommand,
    PppoeCredentialDisposition,
    ensure_pppoe_credential,
)
from tests.test_pppoe_auto_generation import (
    _seed_pppoe_settings,
    _set_subscriber_number,
)

SECRET_PLAIN = "Zq9-distinct-plaintext-77"


def _setting(db, key: str, value: str) -> None:
    db.add(
        DomainSetting(
            domain=SettingDomain.radius,
            key=key,
            value_type=SettingValueType.string,
            value_text=value,
            is_active=True,
        )
    )
    db.commit()


def _sub(db, subscriber, offer, status):
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 12, 1, tzinfo=UTC),
    )
    db.add(sub)
    db.flush()
    return sub


def _cred(db, subscriber, sub, username, *, active=True, secret="old-secret"):  # noqa: S107
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        subscription_id=sub.id if sub is not None else None,
        username=username,
        secret_hash=encrypt_credential(secret),
        is_active=active,
    )
    db.add(credential)
    db.commit()
    return credential


def _ensure(db, subscriber, sub):
    return ensure_pppoe_credential(
        db,
        EnsurePppoeCredentialCommand(
            subscriber_id=subscriber.id, subscription_id=sub.id
        ),
    )


@pytest.fixture
def numbered(db_session, subscriber):
    _seed_pppoe_settings(db_session, start=70000)
    _set_subscriber_number(db_session, subscriber, "SUB-070000")
    return subscriber


@pytest.fixture
def inherit_on(db_session, numbered):
    _setting(db_session, "pppoe_credential_on_new_service", "inherit")


# --------------------------------------------------------------------------
# ensure_pppoe_credential: inherit rule
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ended", [SubscriptionStatus.canceled, SubscriptionStatus.expired]
)
def test_t1_t2_inherit_from_ended_service_keeps_username_and_secret(
    db_session, numbered, catalog_offer, inherit_on, ended
):
    old = _sub(db_session, numbered, catalog_offer, ended)
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _cred(db_session, numbered, old, "10070000")
    secret_before = credential.secret_hash

    outcome = _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    db_session.refresh(new)
    assert outcome.credential_id == credential.id
    assert outcome.username == "10070000"
    assert outcome.disposition is PppoeCredentialDisposition.rebound_replacement
    assert credential.subscription_id == new.id
    assert credential.secret_hash == secret_before
    assert new.login == "10070000"
    assert db_session.query(AccessCredential).count() == 1


def test_t3_cancel_first_inactive_credential_reactivates_with_same_secret(
    db_session, numbered, catalog_offer, inherit_on
):
    old = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.canceled)
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _cred(db_session, numbered, old, "10070000", active=False)
    secret_before = credential.secret_hash

    outcome = _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    assert outcome.credential_id == credential.id
    assert credential.is_active is True
    assert credential.secret_hash == secret_before


def test_t3b_reactivation_path_keeps_secret_in_inherit_mode(
    db_session, numbered, catalog_offer, inherit_on
):
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _cred(db_session, numbered, None, "10070000", active=False)
    secret_before = credential.secret_hash

    outcome = _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    assert outcome.disposition is PppoeCredentialDisposition.reactivated
    assert decrypt_credential(credential.secret_hash) == "old-secret"
    assert credential.secret_hash == secret_before


def test_t4_two_live_services_plus_ended_credential_creates_new(
    db_session, numbered, catalog_offer, inherit_on
):
    ended = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.canceled)
    live = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    ended_cred = _cred(db_session, numbered, ended, "100ended")
    live_cred = _cred(db_session, numbered, live, "100live")

    outcome = _ensure(db_session, numbered, new)

    assert outcome.disposition is PppoeCredentialDisposition.created
    assert outcome.inherit_declined_reason == "other_live_service"
    assert outcome.credential_id not in {ended_cred.id, live_cred.id}
    db_session.refresh(ended_cred)
    assert ended_cred.subscription_id == ended.id


def test_t4b_two_ended_candidates_are_ambiguous(
    db_session, numbered, catalog_offer, inherit_on
):
    a = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.canceled)
    b = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.expired)
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    _cred(db_session, numbered, a, "100a", active=False)
    _cred(db_session, numbered, b, "100b", active=False)

    outcome = _ensure(db_session, numbered, new)

    assert outcome.disposition is PppoeCredentialDisposition.created
    assert outcome.inherit_declined_reason == "ambiguous_candidates"


def test_inherit_never_crosses_customers(
    db_session, numbered, catalog_offer, inherit_on
):
    from app.models.subscriber import Subscriber

    other = Subscriber(
        first_name="Other",
        last_name="Person",
        email=f"o-{uuid.uuid4().hex[:8]}@example.com",
        subscriber_number="SUB-070001",
    )
    db_session.add(other)
    db_session.commit()
    other_old = _sub(db_session, other, catalog_offer, SubscriptionStatus.canceled)
    other_cred = _cred(db_session, other, other_old, "10070001")
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)

    outcome = _ensure(db_session, numbered, new)

    db_session.refresh(other_cred)
    assert outcome.credential_id != other_cred.id
    assert other_cred.subscriber_id == other.id
    assert other_cred.subscription_id == other_old.id


def test_inherit_statuses_setting_narrows_the_ended_set(
    db_session, numbered, catalog_offer, inherit_on
):
    _setting(db_session, "pppoe_credential_inherit_from_statuses", "disabled")
    old = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.canceled)
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _cred(db_session, numbered, old, "legacy-name", active=False)

    outcome = _ensure(db_session, numbered, new)

    assert outcome.credential_id != credential.id


@pytest.mark.parametrize(
    "ended", [SubscriptionStatus.canceled, SubscriptionStatus.expired]
)
def test_t5_default_new_mode_is_unchanged(db_session, numbered, catalog_offer, ended):
    """(G) Default `new`: an ended predecessor's credential is never inherited
    (created-then-ended order) and the cancel-first reactivation still
    overwrites the secret exactly as before."""
    old = _sub(db_session, numbered, catalog_offer, ended)
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    active_cred = _cred(db_session, numbered, old, "10070000", active=True)

    outcome = _ensure(db_session, numbered, new)
    assert outcome.disposition is PppoeCredentialDisposition.created
    assert outcome.inherit_declined_reason is None
    assert outcome.credential_id != active_cred.id

    other_new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    active_cred.is_active = False
    db_session.commit()
    # New username scheme collides with the inactive credential: reactivated.
    reactivated = _ensure(db_session, numbered, other_new)
    db_session.refresh(active_cred)
    assert reactivated.inherit_declined_reason is None
    assert reactivated.disposition in {
        PppoeCredentialDisposition.created,
        PppoeCredentialDisposition.reactivated,
    }


def test_t5b_default_new_mode_reactivation_overwrites_secret(
    db_session, numbered, catalog_offer
):
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    credential = _cred(db_session, numbered, None, "10070000", active=False)

    outcome = _ensure(db_session, numbered, new)

    db_session.refresh(credential)
    assert outcome.disposition is PppoeCredentialDisposition.reactivated
    assert decrypt_credential(credential.secret_hash) != "old-secret"


def test_t6_ask_is_rejected_by_the_setting_spec(db_session, numbered):
    spec = settings_spec.get_spec(
        SettingDomain.radius, "pppoe_credential_on_new_service"
    )
    assert spec is not None
    assert spec.default == "new"
    assert spec.allowed == {"new", "inherit"}
    # A stored "ask" resolves to the safe default, not to a new mode.
    from app.services.pppoe_credentials import _resolve_radius_setting

    _setting(db_session, "pppoe_credential_on_new_service", "ask")
    assert (
        _resolve_radius_setting(db_session, "pppoe_credential_on_new_service") == "new"
    )
    disconnect = settings_spec.get_spec(
        SettingDomain.radius, "pppoe_credential_change_disconnect"
    )
    assert disconnect is not None and disconnect.default == "immediate_after_projection"


def test_staged_typed_credential_is_activated_as_typed(
    db_session, numbered, catalog_offer
):
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    staged = AccessCredential(
        subscriber_id=numbered.id,
        subscription_id=new.id,
        username="typed-login",
        secret_hash=encrypt_credential(SECRET_PLAIN),
        is_active=False,
    )
    db_session.add(staged)
    db_session.commit()

    outcome = _ensure(db_session, numbered, new)

    db_session.refresh(staged)
    db_session.refresh(new)
    assert outcome.credential_id == staged.id
    assert staged.is_active is True
    assert new.login == "typed-login"
    assert decrypt_credential(staged.secret_hash) == SECRET_PLAIN


# --------------------------------------------------------------------------
# create form: typed login/password honoured
# --------------------------------------------------------------------------


def test_create_form_login_and_password_survive_activation(
    db_session, numbered, catalog_offer
):
    created = web.create_subscription_with_audit(
        db_session,
        {"account_id": numbered.id, "offer_id": catalog_offer.id, "status": "pending"},
        FormData([("login", "typed-login"), ("service_password", SECRET_PLAIN)]),
        None,
        None,
    )

    outcome = _ensure(db_session, numbered, created)

    credential = db_session.get(AccessCredential, outcome.credential_id)
    assert credential.username == "typed-login"
    assert auth_flow_service.verify_service_secret(SECRET_PLAIN, credential.secret_hash)
    db_session.refresh(created)
    assert created.login == "typed-login"


def test_create_form_refuses_a_login_owned_by_another_credential(
    db_session, numbered, catalog_offer
):
    other = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    _cred(db_session, numbered, other, "taken-login")
    before = db_session.query(Subscription).count()

    with pytest.raises(ValueError, match="already used"):
        web.create_subscription_with_audit(
            db_session,
            {
                "account_id": numbered.id,
                "offer_id": catalog_offer.id,
                "status": "pending",
            },
            FormData([("login", "taken-login")]),
            None,
            None,
        )
    assert db_session.query(Subscription).count() == before


# --------------------------------------------------------------------------
# edit path
# --------------------------------------------------------------------------


@pytest.fixture
def two_services(db_session, numbered, catalog_offer):
    a = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.stopped)
    b = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    a.login, b.login = "login-a", "login-b"
    cred_a = _cred(db_session, numbered, a, "login-a", secret="secret-a")
    cred_b = _cred(db_session, numbered, b, "login-b", secret="secret-b")
    # Service A is the newest credential: the old fallback would pick it.
    cred_a.created_at = cred_b.created_at.replace(year=cred_b.created_at.year + 1)
    db_session.commit()
    return a, b, cred_a, cred_b


def _edit(db, sub, payload, password="", **kwargs):
    feedback: list[str] = []
    kwargs.setdefault("credential_update_allowed", True)
    result = web.update_subscription_with_audit(
        db,
        str(sub.id),
        payload,
        password,
        [],
        [],
        None,
        "actor-1",
        feedback=feedback,
        **kwargs,
    )
    return result, feedback


@pytest.fixture
def radius_ok():
    with (
        patch(
            "app.services.radius_population.reconcile_usernames",
            return_value={"projection_complete": True, "projection_targets": 1},
        ) as reconcile,
        patch(
            "app.services.enforcement.disconnect_subscription_sessions",
            return_value=1,
        ) as disconnect,
        patch.object(web, "_reconcile_active_subscription_after_credential_sync"),
    ):
        yield reconcile, disconnect


def test_t7_edit_renames_only_this_services_credential(
    db_session, two_services, radius_ok
):
    a, b, cred_a, cred_b = two_services

    _edit(db_session, b, {"login": "login-b2"})

    db_session.refresh(cred_a)
    db_session.refresh(cred_b)
    assert cred_b.username == "login-b2"
    assert cred_a.username == "login-a"
    assert decrypt_credential(cred_a.secret_hash) == "secret-a"


def test_t8_edit_requests_old_and_new_username_from_radius(
    db_session, two_services, radius_ok
):
    _, b, _, _ = two_services
    reconcile, _ = radius_ok

    _edit(db_session, b, {"login": "login-b2"})

    args, kwargs = reconcile.call_args
    assert set(args[0]) == {"login-b", "login-b2"}
    assert kwargs["dry_run"] is False


def test_t9_projection_failure_warns_audits_and_does_not_disconnect(
    db_session, two_services, radius_ok
):
    _, b, _, _ = two_services
    reconcile, disconnect = radius_ok
    reconcile.side_effect = RuntimeError("radius down")
    audit: list[dict] = []
    with patch.object(
        web, "record_audit_event", side_effect=lambda *a, **k: audit.append(k)
    ):
        _, feedback = _edit(db_session, b, {"login": "login-b2"})

    assert feedback and "RADIUS" in feedback[0]
    assert audit[-1]["metadata"]["radius_projected"] is False
    assert audit[-1]["metadata"]["username_changed"] is True
    disconnect.assert_not_called()


def test_t9b_incomplete_projection_is_a_failure(db_session, two_services, radius_ok):
    _, b, _, _ = two_services
    reconcile, disconnect = radius_ok
    reconcile.return_value = {"projection_complete": True, "projection_targets": 0}

    _, feedback = _edit(db_session, b, {"login": "login-b2"})

    assert feedback
    disconnect.assert_not_called()


def test_t10_projection_success_disconnects_the_old_username(
    db_session, two_services, radius_ok
):
    _, b, _, _ = two_services
    _, disconnect = radius_ok

    _, feedback = _edit(db_session, b, {"login": "login-b2"})

    assert feedback == []
    disconnect.assert_called_once()
    assert disconnect.call_args.kwargs["login"] == "login-b"


def test_t10b_disconnect_setting_none_skips_disconnect(
    db_session, two_services, radius_ok
):
    _, b, _, _ = two_services
    _, disconnect = radius_ok
    _setting(db_session, "pppoe_credential_change_disconnect", "none")

    _edit(db_session, b, {"login": "login-b2"})

    disconnect.assert_not_called()


def test_t10c_password_change_disconnects_the_session_of_the_login(
    db_session, two_services, radius_ok
):
    _, b, _, cred_b = two_services
    _, disconnect = radius_ok

    _edit(db_session, b, {"login": "login-b"}, password=SECRET_PLAIN)

    assert disconnect.call_args.kwargs["login"] == "login-b"
    db_session.refresh(cred_b)
    assert auth_flow_service.verify_service_secret(SECRET_PLAIN, cred_b.secret_hash)


def test_t11_edit_context_shows_this_services_credential(db_session, two_services):
    a, b, _, _ = two_services

    context = web.subscription_form_context(
        db_session,
        {
            "id": str(b.id),
            "subscriber_id": str(b.subscriber_id),
            "account_id": str(b.subscriber_id),
            "login": "login-b",
            "service_password": "",
        },
    )

    assert context["current_service_login"] == "login-b"
    assert context["current_service_password"] == "secret-b"


def test_t12_actor_without_credential_permission_changes_nothing(
    db_session, two_services, radius_ok
):
    _, b, _, cred_b = two_services
    reconcile, disconnect = radius_ok

    with pytest.raises(ValueError, match="subscription:credential:update"):
        _edit(
            db_session,
            b,
            {"login": "login-b2"},
            password=SECRET_PLAIN,
            credential_update_allowed=False,
        )

    db_session.refresh(cred_b)
    db_session.refresh(b)
    assert cred_b.username == "login-b"
    assert decrypt_credential(cred_b.secret_hash) == "secret-b"
    assert b.login == "login-b"
    reconcile.assert_not_called()
    disconnect.assert_not_called()


def test_default_is_fail_closed_for_credential_changes(db_session, two_services):
    _, b, _, _ = two_services
    with pytest.raises(ValueError, match="permission"):
        web.update_subscription_with_audit(
            db_session, str(b.id), {"login": "x-login"}, "", [], [], None, None
        )


def test_unchanged_login_needs_no_credential_permission(
    db_session, two_services, radius_ok
):
    _, b, _, _ = two_services
    reconcile, _ = radius_ok

    _edit(db_session, b, {"login": "login-b"}, credential_update_allowed=False)

    reconcile.assert_not_called()


def test_edit_refuses_when_service_has_no_bound_credential(
    db_session, numbered, catalog_offer, radius_ok
):
    own = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.active)
    other = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.stopped)
    other_cred = _cred(db_session, numbered, other, "other-login")

    with pytest.raises(ValueError, match="no credential of its own"):
        _edit(db_session, own, {"login": "new-login"})

    db_session.refresh(other_cred)
    assert other_cred.username == "other-login"


def test_edit_refuses_while_plan_change_is_provisioning(
    db_session, two_services, radius_ok, catalog_offer
):
    from datetime import date

    from app.models.subscription_change import (
        SubscriptionChangeExecutionState,
        SubscriptionChangeRequest,
    )

    _, b, _, cred_b = two_services
    db_session.add(
        SubscriptionChangeRequest(
            subscription_id=b.id,
            current_offer_id=catalog_offer.id,
            requested_offer_id=catalog_offer.id,
            effective_date=date.today(),
            execution_state=SubscriptionChangeExecutionState.provisioning,
        )
    )
    db_session.commit()

    with pytest.raises(ValueError, match="plan change is provisioning"):
        _edit(db_session, b, {"login": "login-b2"})

    db_session.refresh(cred_b)
    assert cred_b.username == "login-b"


def test_edit_on_pending_service_stages_typed_credential(
    db_session, numbered, catalog_offer, radius_ok
):
    reconcile, _ = radius_ok
    pending = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)

    _edit(db_session, pending, {"login": "typed-login"}, password=SECRET_PLAIN)

    staged = (
        db_session.query(AccessCredential)
        .filter(AccessCredential.subscription_id == pending.id)
        .one()
    )
    assert staged.is_active is False
    assert staged.username == "typed-login"
    reconcile.assert_not_called()


# --------------------------------------------------------------------------
# workflow handler
# --------------------------------------------------------------------------


def test_handler_redirects_to_edit_page_with_warning(
    db_session, numbered, catalog_offer
):
    sub = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    db_session.commit()

    def fake_update(*args, **kwargs):
        assert kwargs["credential_update_allowed"] is True
        kwargs["feedback"].append("RADIUS update pending")
        return sub

    with patch.object(web, "update_subscription_with_audit", side_effect=fake_update):
        result = workflows.handle_subscription_update_form(
            db_session,
            subscription_id=str(sub.id),
            form=FormData(
                {
                    "subscriber_id": str(sub.subscriber_id),
                    "offer_id": str(sub.offer_id),
                    "status": sub.status.value,
                    "billing_mode": sub.billing_mode.value,
                    "contract_term": sub.contract_term.value,
                }
            ),
            request=None,
            actor_id="a",
            credential_update_allowed=True,
        )

    assert result["redirect_url"].startswith(
        f"/admin/catalog/subscriptions/{sub.id}/edit?warning="
    )


# --------------------------------------------------------------------------
# no plaintext in logs, events or audit
# --------------------------------------------------------------------------


def _assert_no_plaintext(db, caplog, audit):
    events = [
        json.dumps(row.payload, default=str) for row in db.query(EventStore).all()
    ]
    assert SECRET_PLAIN not in caplog.text
    assert SECRET_PLAIN not in json.dumps(audit, default=str)
    assert all(SECRET_PLAIN not in payload for payload in events)


def test_t13_edit_path_leaks_no_plaintext_password(
    db_session, two_services, radius_ok, caplog
):
    """(G) A typed password never reaches logs, events or audit metadata."""
    caplog.set_level(logging.DEBUG)
    audit: list[dict] = []
    _, b, _, _ = two_services
    with patch.object(
        web, "record_audit_event", side_effect=lambda *a_, **k: audit.append(k)
    ):
        _edit(db_session, b, {"login": "login-b2"}, password=SECRET_PLAIN)
    assert audit and audit[-1]["metadata"]["password_changed"] is True
    _assert_no_plaintext(db_session, caplog, audit)


def test_t13_create_and_activation_leak_no_plaintext_password(
    db_session, numbered, catalog_offer, caplog
):
    caplog.set_level(logging.DEBUG)
    audit: list[dict] = []
    with patch.object(
        web, "record_audit_event", side_effect=lambda *a_, **k: audit.append(k)
    ):
        created = web.create_subscription_with_audit(
            db_session,
            {
                "account_id": numbered.id,
                "offer_id": catalog_offer.id,
                "status": "pending",
            },
            FormData([("login", "typed-login"), ("service_password", SECRET_PLAIN)]),
            None,
            None,
        )
    _ensure(db_session, numbered, created)
    _assert_no_plaintext(db_session, caplog, audit)


def test_restore_of_rebound_away_service_refused_in_inherit_mode(
    db_session, numbered, catalog_offer, inherit_on
):
    from app.services.pppoe_credentials import PppoeCredentialError

    old = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.disabled)
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    old.login = "10070000"
    credential = _cred(db_session, numbered, old, "10070000")
    _ensure(db_session, numbered, new)  # new inherits; old is rebound away
    old.status = SubscriptionStatus.active  # staff restore the old service
    new.status = SubscriptionStatus.stopped
    db_session.commit()

    with pytest.raises(PppoeCredentialError, match="replacement service"):
        _ensure(db_session, numbered, old)

    db_session.refresh(credential)
    assert credential.subscription_id == new.id
    assert db_session.query(AccessCredential).count() == 1


def test_restore_of_rebound_away_service_issues_new_credential_in_new_mode(
    db_session, numbered, catalog_offer
):
    old = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.disabled)
    new = _sub(db_session, numbered, catalog_offer, SubscriptionStatus.pending)
    old.login = "10070000"
    credential = _cred(db_session, numbered, old, "10070000")
    # Today's rebound_replacement of a disabled predecessor (any mode).
    first = _ensure(db_session, numbered, new)
    assert first.credential_id == credential.id
    old.status = SubscriptionStatus.active
    new.status = SubscriptionStatus.stopped
    db_session.commit()

    outcome = _ensure(db_session, numbered, old)

    db_session.refresh(credential)
    assert credential.subscription_id == new.id
    assert outcome.credential_id != credential.id
    assert outcome.disposition is PppoeCredentialDisposition.created
