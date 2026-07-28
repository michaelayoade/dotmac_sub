from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from jose import jwt
from starlette.requests import Request

from app.api import auth_flow as auth_api
from app.api import crm_referrals as referral_api
from app.models.audit import AuditEvent
from app.models.auth import AuthProvider, UserCredential
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.event_store import EventStore
from app.models.notification import (
    CommunicationIntentRecord,
    Notification,
    NotificationChannel,
    NotificationStatus,
    SuppressionReason,
    SuppressionScope,
)
from app.models.party import (
    Party,
    PartyContactPoint,
    PartyContactPointType,
    PartyContactVerificationStatus,
    PartyIdentityStatus,
)
from app.models.referral_native import Referral
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.auth_flow import CredentialEnrollmentRequest
from app.schemas.referral import (
    ReferralCaptureRequest,
    ReferralSelfServiceAccountCreate,
    ReferralSelfServiceSignupRequest,
)
from app.services import (
    auth_flow,
    communication_eligibility,
    customer_credential_enrollment,
    settings_spec,
    web_customer_auth,
)
from app.services import (
    email as email_service,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.tasks import notifications as notification_tasks
from app.web.customer import auth as customer_auth_web
from tests.referral_program_testkit import ensure_code


@pytest.fixture(autouse=True)
def _allow_immediate_notification_delivery(monkeypatch):
    """Keep enrollment delivery tests independent of quiet-hour timing."""
    monkeypatch.setattr(
        "app.services.notification.quiet_hours_send_at",
        lambda _db: None,
    )


def _email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


def _request(path: str = "/portal/auth/credential-enrollment") -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
        }
    )
    request.state.csrf_token = "csrf"
    return request


def _enable_program(db) -> None:
    db.add(
        DomainSetting(
            domain=SettingDomain.subscriber,
            key="referral_program_enabled",
            value_type=SettingValueType.boolean,
            value_text="true",
            is_active=True,
        )
    )
    db.commit()


def _referrer(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Referral",
        last_name="Owner",
        email=_email("referrer"),
        status=SubscriberStatus.active,
        is_active=True,
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def _deliver(db, sent: dict[str, object]) -> dict[str, int]:
    stats = notification_tasks._deliver_notification_queue_stats(db, batch_size=10)
    body_html = str(sent.get("body_html") or "")
    match = re.search(r"#token=([A-Za-z0-9._-]+)", body_html)
    if match:
        sent["reset_token"] = match.group(1)
    sent["stats"] = stats
    return stats


def _signup(
    db,
    monkeypatch,
    *,
    delivered: bool = True,
    deliver_now: bool = True,
    hard_suppress: bool = False,
):
    sent: dict[str, object] = {}

    def _send(**kwargs):
        if kwargs.get("sensitive_content"):
            sent.update(kwargs)
        return delivered

    monkeypatch.setattr(email_service, "send_email", _send)
    _enable_program(db)
    code = ensure_code(db, _referrer(db).id)
    capture = referral_api.capture_referral(
        ReferralCaptureRequest(
            code=code.code,
            name="Credential Prospect",
            email=_email("capture"),
            phone="0803 111 0099",
        ),
        db,
    )
    account_email = _email("account")
    if hard_suppress:
        communication_eligibility.suppress(
            db,
            channel=NotificationChannel.email,
            address=account_email,
            scope=SuppressionScope.all,
            reason=SuppressionReason.bounce,
        )
        db.commit()
    result = referral_api.signup_referral_account(
        ReferralSelfServiceSignupRequest(
            conversion_token=capture.conversion_token,
            account=ReferralSelfServiceAccountCreate(
                first_name="Ada",
                last_name="Customer",
                email=account_email,
                phone="0804 222 0088",
                city="Abuja",
            ),
        ),
        db,
    )
    referral = db.get(Referral, result.referral_id)
    subscriber = db.get(Subscriber, result.subscriber_id)
    assert referral is not None
    assert subscriber is not None
    if deliver_now:
        _deliver(db, sent)
    return referral, subscriber, result, sent


def _complete(db, token: str, *, username: str | None = None):
    return auth_api.credential_enrollment_endpoint(
        CredentialEnrollmentRequest(
            token=token,
            new_password="Secure-customer-password-42",
            username=username,
        ),
        db,
    )


def _owner_context(reason: str) -> CommandContext:
    command_id = uuid.uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="service:credential-enrollment-test",
        scope=(customer_credential_enrollment.CUSTOMER_CREDENTIAL_ENROLLMENT_SCOPE),
        reason=reason,
        idempotency_key=f"credential-enrollment-test:{command_id}",
    )


def test_enrollment_policy_tunables_are_registered_with_one_settings_owner():
    expected = {
        "user_invite_expiry_minutes": 1440,
        "password_min_length": 8,
        "credential_enrollment_request_limit": 3,
        "credential_enrollment_request_window_seconds": 900,
    }

    for key, default in expected.items():
        spec = settings_spec.get_spec(SettingDomain.auth, key)
        assert spec is not None
        assert spec.default == default


def _set_auth_integer(db, key: str, value: int) -> None:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.auth)
        .filter(DomainSetting.key == key)
        .one_or_none()
    )
    if setting is None:
        setting = DomainSetting(
            domain=SettingDomain.auth,
            key=key,
            value_type=SettingValueType.integer,
            value_text=str(value),
            is_active=True,
        )
        db.add(setting)
    else:
        setting.value_type = SettingValueType.integer
        setting.value_text = str(value)
        setting.is_active = True
    db.commit()


def test_delivery_uses_canonical_invite_lifetime_setting(db_session, monkeypatch):
    _set_auth_integer(db_session, "user_invite_expiry_minutes", 30)

    _, _, _, sent = _signup(db_session, monkeypatch)

    claims = jwt.get_unverified_claims(str(sent["reset_token"]))
    assert claims["exp"] - claims["iat"] == 30 * 60


def test_request_uses_canonical_rate_policy_settings(db_session, monkeypatch):
    _set_auth_integer(db_session, "credential_enrollment_request_limit", 4)
    _set_auth_integer(
        db_session,
        "credential_enrollment_request_window_seconds",
        1200,
    )
    observed: dict[str, int] = {}

    def allow(_key, *, limit, window_seconds):
        observed.update(limit=limit, window_seconds=window_seconds)
        return type(
            "Decision",
            (),
            {"allowed": True, "retry_after_seconds": None},
        )()

    monkeypatch.setattr(customer_credential_enrollment, "allow_operation", allow)

    _signup(db_session, monkeypatch, deliver_now=False)

    assert observed == {"limit": 4, "window_seconds": 1200}


def test_signup_sends_pii_free_capability_without_placeholder_credential(
    db_session, monkeypatch
):
    referral, subscriber, result, sent = _signup(
        db_session, monkeypatch, deliver_now=False
    )

    assert result.enrollment_status == "queued"
    assert result.enrollment_retry_after_seconds is None
    assert sent == {}
    notification = (
        db_session.query(Notification)
        .filter(Notification.subscriber_id == subscriber.id)
        .filter(Notification.event_type == "auth.referral_credential_enrollment")
        .one()
    )
    intent = db_session.get(
        CommunicationIntentRecord, notification.communication_intent_id
    )
    assert intent is not None
    assert notification.status == NotificationStatus.queued
    assert notification.body is None
    assert intent.body is None
    serialized_outbox = f"{intent.metadata_} {notification.metadata_}"
    assert subscriber.email not in serialized_outbox
    assert "token" not in serialized_outbox.lower()
    assert "password" not in serialized_outbox.lower()
    queued_at = datetime.now(UTC) - timedelta(hours=2)
    notification.created_at = queued_at
    db_session.commit()

    stats = _deliver(db_session, sent)

    assert stats["delivered"] >= 1
    assert sent["to_email"] == subscriber.email
    assert sent["track"] is False
    assert sent["sensitive_content"] is True
    assert sent["activity"] == "auth_user_invite"
    assert (
        db_session.query(UserCredential)
        .filter(UserCredential.subscriber_id == subscriber.id)
        .count()
        == 0
    )
    token = str(sent["reset_token"])
    db_session.refresh(notification)
    assert notification.status == NotificationStatus.delivered
    assert notification.body is None
    assert token not in str(notification.metadata_)
    assert token not in str(intent.metadata_)
    request_audits = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "auth.customer_credential_enrollment_requested")
        .all()
    )
    assert request_audits
    assert all(token not in str(event.metadata_) for event in request_audits)
    request_event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "customer_credential_enrollment.requested")
        .one()
    )
    serialized_event = str(request_event.payload).lower()
    assert subscriber.email.lower() not in serialized_event
    assert token not in serialized_event
    assert "password" not in serialized_event
    claims = jwt.get_unverified_claims(token)
    assert claims["typ"] == "referral_credential_enrollment"
    assert claims["referral_id"] == str(referral.id)
    assert claims["subscriber_id"] == str(subscriber.id)
    assert claims["email_sha256"] != subscriber.email
    assert claims["iat"] > int((queued_at + timedelta(hours=1)).timestamp())
    assert claims["exp"] - claims["iat"] == 24 * 60 * 60
    assert set(claims) == {
        "typ",
        "iss",
        "ver",
        "sub",
        "referral_id",
        "referred_party_id",
        "referred_lead_id",
        "subscriber_id",
        "email_sha256",
        "iat",
        "exp",
    }
    serialized_claims = str(claims).lower()
    assert subscriber.email.lower() not in serialized_claims
    assert subscriber.first_name not in claims.values()
    assert subscriber.last_name not in claims.values()
    assert "password" not in serialized_claims


def test_completion_creates_chosen_credential_and_preserves_account_identity_states(
    db_session, monkeypatch
):
    referral, subscriber, _, sent = _signup(db_session, monkeypatch, deliver_now=False)
    party = db_session.get(Party, referral.referred_party_id)
    contact = (
        db_session.query(PartyContactPoint)
        .filter(PartyContactPoint.party_id == referral.referred_party_id)
        .filter(PartyContactPoint.channel_type == PartyContactPointType.email.value)
        .one()
    )
    assert party is not None
    assert party.status == PartyIdentityStatus.quarantined.value
    assert (
        contact.verification_status == PartyContactVerificationStatus.unverified.value
    )

    # Billing enforcement remains an independent lifecycle owner even if it
    # changes state between account creation and credential enrollment.
    subscriber.status = SubscriberStatus.blocked
    db_session.commit()
    stats = _deliver(db_session, sent)
    assert stats["delivered"] >= 1

    completed = _complete(db_session, str(sent["reset_token"]))

    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.subscriber_id == subscriber.id)
        .filter(UserCredential.provider == AuthProvider.local)
        .one()
    )
    assert completed.subscriber_id == subscriber.id
    assert completed.username == subscriber.email.lower()
    assert auth_flow.verify_password(
        "Secure-customer-password-42", credential.password_hash
    )
    assert credential.must_change_password is False
    assert credential.is_active is True
    db_session.refresh(subscriber)
    db_session.refresh(party)
    db_session.refresh(contact)
    assert subscriber.email_verified is True
    assert subscriber.status == SubscriberStatus.blocked
    assert subscriber.lifecycle_override_status is None
    assert party.status == PartyIdentityStatus.quarantined.value
    assert (
        contact.verification_status == PartyContactVerificationStatus.unverified.value
    )


def test_enrollment_capability_is_single_use_and_does_not_change_password_on_replay(
    db_session, monkeypatch
):
    _, subscriber, _, sent = _signup(db_session, monkeypatch)
    token = str(sent["reset_token"])
    _complete(db_session, token)

    with pytest.raises(HTTPException) as exc:
        auth_api.credential_enrollment_endpoint(
            CredentialEnrollmentRequest(
                token=token,
                new_password="A-different-secure-password-84",
            ),
            db_session,
        )
    assert exc.value.status_code == 401
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.subscriber_id == subscriber.id)
        .one()
    )
    assert auth_flow.verify_password(
        "Secure-customer-password-42", credential.password_hash
    )
    assert not auth_flow.verify_password(
        "A-different-secure-password-84", credential.password_hash
    )


def test_request_replay_converges_on_one_delivery_intent(db_session, monkeypatch):
    referral, subscriber, _, _ = _signup(
        db_session,
        monkeypatch,
        deliver_now=False,
    )
    referral_id = referral.id
    referred_party_id = referral.referred_party_id
    referred_lead_id = referral.referred_lead_id
    subscriber_id = subscriber.id
    db_session.commit()

    result = customer_credential_enrollment.request_referral_enrollment(
        db_session,
        customer_credential_enrollment.RequestReferralEnrollmentCommand(
            context=_owner_context("Retry an accepted enrollment request"),
            referral_id=referral_id,
            referred_party_id=referred_party_id,
            referred_lead_id=referred_lead_id,
            subscriber_id=subscriber_id,
        ),
    )

    assert result.status == "queued"
    assert (
        db_session.query(CommunicationIntentRecord)
        .filter(
            CommunicationIntentRecord.event_type
            == "auth.referral_credential_enrollment"
        )
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "customer_credential_enrollment.requested")
        .count()
        == 2
    )


def test_completion_event_repairs_auth_cache_strictly(db_session, monkeypatch):
    _, subscriber, _, sent = _signup(db_session, monkeypatch)
    calls: list[tuple[str, str]] = []
    from app.services.events.handlers import credential_session_projection

    monkeypatch.setattr(
        credential_session_projection.auth_cache,
        "invalidate_principal_strict",
        lambda principal_type, principal_id: calls.append(
            (principal_type, principal_id)
        ),
    )

    _complete(db_session, str(sent["reset_token"]))

    assert calls == [("subscriber", str(subscriber.id))]
    completion_event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "customer_credential_enrollment.completed")
        .one()
    )
    serialized_event = str(completion_event.payload).lower()
    assert subscriber.email.lower() not in serialized_event
    assert str(sent["reset_token"]) not in serialized_event
    assert "password_hash" not in serialized_event


def test_completion_rolls_back_credential_and_verification_when_event_staging_fails(
    db_session, monkeypatch
):
    _, subscriber, _, sent = _signup(db_session, monkeypatch)

    def fail_event_staging(*_args, **_kwargs):
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(
        customer_credential_enrollment,
        "emit_event",
        fail_event_staging,
    )
    with pytest.raises(RuntimeError, match="event store unavailable"):
        _complete(db_session, str(sent["reset_token"]))

    db_session.refresh(subscriber)
    assert subscriber.email_verified is False
    assert (
        db_session.query(UserCredential)
        .filter(UserCredential.subscriber_id == subscriber.id)
        .count()
        == 0
    )


def test_enrollment_rejects_tampering_expiry_and_changed_email(db_session, monkeypatch):
    referral, subscriber, _, sent = _signup(db_session, monkeypatch)
    token = str(sent["reset_token"])
    head, body, signature = token.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    tampered = ".".join((head, body, replacement + signature[1:]))
    with pytest.raises(HTTPException) as exc:
        _complete(db_session, tampered)
    assert exc.value.status_code == 401

    context = customer_credential_enrollment.EnrollmentContext(
        referral_id=referral.id,
        referred_party_id=referral.referred_party_id,
        referred_lead_id=referral.referred_lead_id,
        subscriber_id=subscriber.id,
        email_digest=customer_credential_enrollment._email_digest(subscriber.email),
    )
    expired, _ = customer_credential_enrollment._issue_token(
        db_session,
        context,
        now=datetime.now(UTC) - timedelta(days=2),
    )
    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(HTTPException) as exc:
        _complete(db_session, expired)
    assert exc.value.status_code == 401

    subscriber.email = _email("changed")
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        _complete(db_session, token)
    assert exc.value.status_code == 401
    assert db_session.query(UserCredential).count() == 0


def test_username_collision_is_rejected_without_partial_verification(
    db_session, monkeypatch
):
    _, subscriber, _, sent = _signup(db_session, monkeypatch)
    owner = _referrer(db_session)
    username = _email("taken").lower()
    db_session.add(
        UserCredential(
            subscriber_id=owner.id,
            provider=AuthProvider.local,
            username=username,
            password_hash=auth_flow.hash_password("Existing-password-42"),
            is_active=True,
        )
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        _complete(db_session, str(sent["reset_token"]), username=username.upper())
    assert exc.value.status_code == 409
    db_session.refresh(subscriber)
    assert subscriber.email_verified is False
    assert (
        db_session.query(UserCredential)
        .filter(UserCredential.subscriber_id == subscriber.id)
        .count()
        == 0
    )


def test_delivery_failure_retries_after_account_is_safely_committed(
    db_session, monkeypatch
):
    _, subscriber, result, sent = _signup(db_session, monkeypatch, delivered=False)

    assert sent["to_email"] == subscriber.email
    assert result.enrollment_status == "queued"
    notification = (
        db_session.query(Notification)
        .filter(Notification.subscriber_id == subscriber.id)
        .filter(Notification.event_type == "auth.referral_credential_enrollment")
        .one()
    )
    assert notification.status == NotificationStatus.failed
    assert notification.retry_count == 1
    assert notification.send_at is not None
    assert notification.body is None
    assert sent["stats"]["retried"] >= 1
    assert db_session.get(Subscriber, subscriber.id) is not None
    assert db_session.query(UserCredential).count() == 0

    first_token = str(sent["reset_token"])
    for other in (
        db_session.query(Notification)
        .filter(Notification.id != notification.id)
        .filter(Notification.status == NotificationStatus.failed)
        .all()
    ):
        other.status = NotificationStatus.canceled
    notification.send_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    original_issue = customer_credential_enrollment._issue_token

    def _issue_later(db, context, *, now=None):
        return original_issue(
            db,
            context,
            now=datetime.now(UTC) + timedelta(minutes=2),
        )

    retry_sent: dict[str, object] = {}

    def _retry_send(**kwargs):
        retry_sent.update(kwargs)
        return True

    monkeypatch.setattr(customer_credential_enrollment, "_issue_token", _issue_later)
    monkeypatch.setattr(email_service, "send_email", _retry_send)
    retry_stats = _deliver(db_session, retry_sent)

    assert retry_stats["delivered"] == 1
    assert retry_sent["reset_token"] != first_token
    db_session.refresh(notification)
    assert notification.status == NotificationStatus.delivered
    assert notification.body is None
    assert retry_sent["reset_token"] not in str(notification.metadata_)


def test_context_change_before_delivery_rejects_without_minting_or_sending(
    db_session, monkeypatch
):
    _, subscriber, result, sent = _signup(
        db_session,
        monkeypatch,
        deliver_now=False,
    )
    subscriber.email = _email("changed-before-delivery")
    db_session.commit()

    stats = _deliver(db_session, sent)

    assert result.enrollment_status == "queued"
    assert sent == {"stats": stats}
    notification = (
        db_session.query(Notification)
        .filter(Notification.subscriber_id == subscriber.id)
        .filter(Notification.event_type == "auth.referral_credential_enrollment")
        .one()
    )
    assert notification.status == NotificationStatus.canceled
    assert notification.last_error == (
        "ephemeral_action_rejected:stale_account_context"
    )
    assert stats["materialization_rejected"] == 1
    assert db_session.query(UserCredential).count() == 0


def test_hard_suppression_prevents_delivery_but_keeps_account(db_session, monkeypatch):
    _, subscriber, result, sent = _signup(
        db_session,
        monkeypatch,
        deliver_now=False,
        hard_suppress=True,
    )

    assert result.enrollment_status == "suppressed"
    assert sent == {}
    assert db_session.get(Subscriber, subscriber.id) is not None
    assert db_session.query(UserCredential).count() == 0


def test_credential_enrollment_route_is_public():
    route = next(
        route
        for route in auth_api.router.routes
        if isinstance(route, APIRoute) and route.path == "/auth/credential-enrollment"
    )
    dependency_calls = {
        getattr(dependency.call, "__name__", "")
        for dependency in route.dependant.dependencies
    }
    assert "require_user_auth" not in dependency_calls

    web_routes = {
        (route.path, frozenset(route.methods or set()))
        for route in customer_auth_web.router.routes
        if isinstance(route, APIRoute)
    }
    assert (
        "/portal/auth/credential-enrollment",
        frozenset({"GET"}),
    ) in web_routes
    assert (
        "/portal/auth/credential-enrollment",
        frozenset({"POST"}),
    ) in web_routes


def test_selfcare_enrollment_form_delegates_to_owner(db_session, monkeypatch):
    token = "header.payload.signature"
    page = web_customer_auth.customer_credential_enrollment_page(
        _request(), db_session, token
    )
    body = page.body.decode()
    assert page.status_code == 200
    assert 'action="/portal/auth/credential-enrollment"' in body
    assert 'name="token"' in body
    assert f'value="{token}"' in body
    assert "window.location.hash" in body
    assert 'x-model="token"' in body
    assert "_csrf_token" in body
    db_session.rollback()

    captured: dict[str, object] = {}

    def _complete(db, command):
        captured["db"] = db
        captured["command"] = command
        return object()

    monkeypatch.setattr(
        customer_credential_enrollment,
        "complete_referral_enrollment",
        _complete,
    )
    response = web_customer_auth.customer_credential_enrollment_submit(
        _request(),
        db_session,
        token=token,
        password="Customer-selected-password-42",
        password_confirm="Customer-selected-password-42",
        username="customer.login",
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/portal/auth/login?enrollment=success"
    assert captured["db"] is db_session
    command = captured["command"]
    assert isinstance(
        command,
        customer_credential_enrollment.CompleteReferralEnrollmentCommand,
    )
    assert command.token == token
    assert command.new_password == "Customer-selected-password-42"
    assert command.username == "customer.login"
    assert (
        command.context.scope
        == customer_credential_enrollment.CUSTOMER_CREDENTIAL_ENROLLMENT_SCOPE
    )
