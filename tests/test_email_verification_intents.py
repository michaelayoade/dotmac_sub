"""Fast unit evidence for bounded verification request and JIT delivery."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.notification import CommunicationIntentRecord, Notification
from app.services import auth_flow, email_verification
from app.services.audit_adapter import AuditActor
from app.services.ephemeral_communication_actions import (
    EPHEMERAL_ACTION_METADATA_KEY,
    EphemeralActionRejected,
    materialize_email,
)
from app.services.owner_commands import CommandContext


def _command(db, subscriber):
    subscriber.email = "verify@example.com"
    subscriber.email_verified = False
    subscriber.is_active = True
    subscriber_id = subscriber.id
    db.commit()
    return email_verification.RequestEmailVerificationCommand(
        context=CommandContext.system(
            actor=f"subscriber:{subscriber_id}",
            scope=email_verification.EMAIL_VERIFICATION_SCOPE,
            reason="Unit verification request",
            idempotency_key=str(uuid4()),
        ),
        subscriber_id=subscriber_id,
        actor=AuditActor.user(str(subscriber_id)),
    )


def test_request_queues_only_safe_context_and_replays(
    db_session, subscriber, monkeypatch
):
    command = _command(db_session, subscriber)

    from app.models.subscriber import Subscriber, SubscriberContact

    # Neither a shared contact address nor an opted-in contact may expand this
    # exact Subscriber capability request into another principal or mailbox.
    db_session.add(
        Subscriber(first_name="Shared", last_name="Mailbox", email="verify@example.com")
    )
    db_session.add(
        SubscriberContact(
            subscriber_id=command.subscriber_id,
            email="secondary@example.com",
            receives_notifications=True,
        )
    )
    db_session.commit()

    def no_mint(*args, **kwargs):
        raise AssertionError("Request must never mint a token")

    monkeypatch.setattr(auth_flow, "_issue_email_verification_token", no_mint)
    first = email_verification.request_email_verification(db_session, command=command)
    assert first.queued
    replay = email_verification.request_email_verification(db_session, command=command)
    assert replay.intent_id == first.intent_id
    assert replay.replayed
    records = db_session.scalars(select(CommunicationIntentRecord)).all()
    notifications = db_session.scalars(select(Notification)).all()
    assert len(records) == len(notifications) == 1
    assert records[0].body is None and records[0].subject is None
    assert notifications[0].body is None and notifications[0].subject is None
    assert notifications[0].recipient == "verify@example.com"
    assert notifications[0].audience_id == command.subscriber_id
    assert notifications[0].subscriber_id == command.subscriber_id
    assert records[0].include_reseller is False
    assert "verify@example.com" not in json.dumps(records[0].metadata_)
    audit = db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "auth.email_verification_requested"
        )
    ).all()
    assert len(audit) == 1
    assert audit[0].actor_type.value == "user"
    assert audit[0].actor_id == str(command.subscriber_id)
    assert "verify@example.com" not in str(audit[0].metadata_)


def test_reused_key_with_changed_address_fails_closed(db_session, subscriber):
    command = _command(db_session, subscriber)
    email_verification.request_email_verification(db_session, command=command)
    subscriber.email = "changed@example.com"
    db_session.commit()
    with pytest.raises(
        email_verification.EmailVerificationError, match="different verification"
    ):
        email_verification.request_email_verification(db_session, command=command)
    assert len(db_session.scalars(select(CommunicationIntentRecord)).all()) == 1


@pytest.mark.parametrize("failure_owner", ["audit", "intent"])
def test_failure_rolls_back_all_request_evidence(
    db_session, subscriber, monkeypatch, failure_owner
):
    command = _command(db_session, subscriber)

    def fail(*args, **kwargs):
        raise RuntimeError("Injected storage failure")

    if failure_owner == "audit":
        monkeypatch.setattr(email_verification, "stage_audit_event", fail)
    else:
        # Run real flush-only expansion before failing, proving outbox rollback.
        original = email_verification.communication_intents.submit

        def fail_after_queue(*args, **kwargs):
            original(*args, **kwargs)
            fail()

        monkeypatch.setattr(
            email_verification.communication_intents, "submit", fail_after_queue
        )
    with pytest.raises(RuntimeError, match="Injected"):
        email_verification.request_email_verification(db_session, command=command)
    assert db_session.scalars(select(CommunicationIntentRecord)).all() == []
    assert db_session.scalars(select(Notification)).all() == []
    assert (
        db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "auth.email_verification_requested"
            )
        ).all()
        == []
    )


@pytest.mark.parametrize(
    "drift", ["address", "verified", "purpose", "audience", "ttl", "extra"]
)
def test_worker_refuses_drift_before_mint(db_session, subscriber, monkeypatch, drift):
    command = _command(db_session, subscriber)
    email_verification.request_email_verification(db_session, command=command)
    notification = db_session.scalars(select(Notification)).one()
    if drift == "address":
        subscriber.email = "other@example.com"
    elif drift == "verified":
        subscriber.email_verified = True
    elif drift == "audience":
        notification.audience_id = uuid4()
    else:
        metadata = dict(notification.metadata_)
        envelope = dict(metadata[EPHEMERAL_ACTION_METADATA_KEY])
        context = dict(envelope["context"])
        context[
            {"purpose": "purpose", "ttl": "ttl_minutes", "extra": "bearer"}[drift]
        ] = {"purpose": "password_reset", "ttl": True, "extra": "forbidden"}[drift]
        envelope["context"] = context
        metadata[EPHEMERAL_ACTION_METADATA_KEY] = envelope
        notification.metadata_ = metadata
    db_session.flush()

    def no_mint(*args, **kwargs):
        raise AssertionError("Rejected context must never mint")

    monkeypatch.setattr(auth_flow, "_issue_email_verification_token", no_mint)
    with pytest.raises(EphemeralActionRejected):
        materialize_email(db_session, notification)


def test_worker_renders_only_in_memory(db_session, subscriber, monkeypatch):
    command = _command(db_session, subscriber)
    email_verification.request_email_verification(db_session, command=command)
    notification = db_session.scalars(select(Notification)).one()
    monkeypatch.setattr(
        auth_flow,
        "_issue_email_verification_token",
        lambda *args, **kwargs: "test-bearer-never-persist",
    )
    content = materialize_email(db_session, notification)
    assert "test-bearer-never-persist" in content.body_text
    assert "?token=" in content.body_text
    db_session.flush()
    assert notification.body is None
    assert "test-bearer-never-persist" not in json.dumps(notification.metadata_)


@pytest.mark.parametrize("flushed", [False, True])
def test_public_owner_refuses_caller_transaction_without_discard(
    db_session, subscriber, flushed
):
    command = _command(db_session, subscriber)
    subscriber.first_name = "Keep my pending change"
    if flushed:
        db_session.flush()
    with pytest.raises(Exception, match="transaction"):
        email_verification.request_email_verification(db_session, command=command)
    assert subscriber.first_name == "Keep my pending change"
    assert db_session.in_transaction()


@pytest.mark.parametrize("adapter", ["api", "customer", "reseller"])
@pytest.mark.parametrize("flushed", [False, True])
def test_resend_adapter_keeps_caller_writes_and_uses_independent_session(
    db_session, subscriber, monkeypatch, adapter, flushed
):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.api import auth_flow as api
    from app.services import rate_limiter_adapter, web_reseller_routes
    from app.services.db_session_adapter import db_session_adapter
    from app.web.customer import routes

    target = subscriber.id
    subscriber.first_name = "Preserve caller work"
    if flushed:
        db_session.flush()
    transaction = db_session.get_transaction()
    owner_session = object()
    calls = []

    @contextmanager
    def independent_session():
        yield owner_session

    def queue(db, *, command):
        assert db is owner_session
        assert command.subscriber_id == target
        assert command.context.actor == f"subscriber:{target}"
        calls.append(command)
        return email_verification.EmailVerificationOutcome(uuid4(), True)

    monkeypatch.setattr(
        db_session_adapter, "owner_command_session", independent_session
    )
    monkeypatch.setattr(email_verification, "request_email_verification", queue)
    request = Request(
        {"type": "http", "method": "POST", "path": "/profile", "headers": []}
    )
    if adapter == "api":
        monkeypatch.setattr(
            rate_limiter_adapter,
            "allow_operation",
            lambda *args, **kwargs: SimpleNamespace(allowed=True),
        )
        assert api.resend_verification_email_endpoint(
            auth={"principal_id": str(target), "principal_type": "subscriber"},
            db=db_session,
        ).sent
    elif adapter == "customer":
        monkeypatch.setattr(
            routes,
            "get_current_customer_from_request",
            lambda *args: {"id": str(target)},
        )
        monkeypatch.setattr(routes, "_is_read_only_customer", lambda *args: False)
        monkeypatch.setattr(
            routes, "optional_customer_subscriber_id", lambda *args: target
        )
        assert (
            "verify_sent=1"
            in routes.customer_resend_email_verification(request, db_session).headers[
                "location"
            ]
        )
    else:
        monkeypatch.setattr(
            web_reseller_routes,
            "_require_reseller_context",
            lambda *args: {"subscriber": subscriber},
        )
        assert (
            "verify_sent=1"
            in web_reseller_routes.reseller_resend_email_verification(
                request, db_session
            ).headers["location"]
        )
    assert len(calls) == 1
    assert db_session.get_transaction() is transaction
    assert subscriber.first_name == "Preserve caller work"


@pytest.mark.parametrize("principal_type", ["system_user", "reseller_user"])
def test_api_refuses_non_subscriber_verification(principal_type, monkeypatch):
    from app.api import auth_flow as api

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Unrelated principal UUID must not be treated as Subscriber"
        )

    monkeypatch.setattr(email_verification, "request_email_verification", forbidden)
    assert not api.resend_verification_email_endpoint(
        auth={"principal_id": str(uuid4()), "principal_type": principal_type}, db=None
    ).sent


@pytest.mark.parametrize("failure", [False, True])
def test_profile_email_and_verification_evidence_are_atomic(
    db_session, subscriber, monkeypatch, failure
):
    from app.services import customer_portal_profile_commands as profile

    monkeypatch.setattr(
        "app.services.customer_location_requests.geocode_service_address",
        lambda *args, **kwargs: None,
    )
    command = profile.UpdateCustomerProfileCommand(
        context=CommandContext.system(
            actor=str(subscriber.id),
            scope=profile.PORTAL_PROFILE_WRITE_SCOPE,
            reason="Verify atomic profile consequence",
        ),
        subscriber_id=subscriber.id,
        actor=AuditActor.user(str(subscriber.id)),
        first_name=subscriber.first_name,
        last_name=subscriber.last_name,
        email="new-profile@example.com",
        billing_notifications=True,
        sms_updates=True,
    )
    old_email = subscriber.email
    subscriber.email_verified = True
    db_session.commit()
    if failure:

        def fail(*args, **kwargs):
            raise RuntimeError("verification audit unavailable")

        monkeypatch.setattr(email_verification, "stage_audit_event", fail)
        with pytest.raises(RuntimeError, match="verification audit"):
            profile.update_customer_profile(db_session, command=command)
        db_session.refresh(subscriber)
        assert subscriber.email == old_email
        assert subscriber.email_verified
        assert db_session.scalars(select(Notification)).all() == []
    else:
        outcome = profile.update_customer_profile(db_session, command=command)
        assert outcome.email_changed
        db_session.refresh(subscriber)
        assert subscriber.email == "new-profile@example.com"
        assert not subscriber.email_verified
        assert (
            db_session.scalars(
                select(Notification).where(
                    Notification.event_type == "auth.email_verification"
                )
            )
            .one()
            .recipient
            == subscriber.email
        )


@pytest.mark.parametrize("transport_fails", [False, True])
def test_worker_sensitive_transport_never_persists_or_logs_bearer(
    db_session, subscriber, monkeypatch, caplog, transport_fails
):
    from app.models.notification import NotificationDelivery
    from app.services import email
    from app.tasks.notifications import _deliver_notification_queue_stats

    command = _command(db_session, subscriber)
    bearer = "sensitive-worker-bearer-for-test"

    def unexpected_transport(**kwargs):
        raise AssertionError("Request must not call transport before commit")

    monkeypatch.setattr(email, "send_email", unexpected_transport)
    email_verification.request_email_verification(db_session, command=command)
    captured = []

    def send(**kwargs):
        assert kwargs["track"] is False
        assert kwargs["sensitive_content"] is True
        assert bearer in kwargs["body_text"]
        captured.append(True)
        if transport_fails:
            raise RuntimeError(bearer)
        return True

    monkeypatch.setattr(email, "send_email", send)
    monkeypatch.setattr(
        auth_flow, "_issue_email_verification_token", lambda *args, **kwargs: bearer
    )
    _deliver_notification_queue_stats(db_session, batch_size=10)
    assert captured == [True]
    notification = db_session.scalars(select(Notification)).one()
    assert notification.body is None
    assert bearer not in str(notification.metadata_)
    assert bearer not in str(notification.last_error)
    assert bearer not in caplog.text
    for row in db_session.scalars(select(NotificationDelivery)).all():
        assert bearer not in str(row.response_body)
    for row in db_session.scalars(select(AuditEvent)).all():
        assert bearer not in str(row.metadata_)


@pytest.mark.parametrize("adapter", ["api", "customer", "reseller", "profile"])
def test_impersonation_without_trusted_actor_is_refused_before_owner(
    monkeypatch, adapter
):
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.api import auth_flow as api
    from app.services import customer_portal_profile_commands as profile
    from app.services import web_reseller_routes
    from app.web.customer import routes

    target = uuid4()

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Impersonation with missing actual actor must not enter owner"
        )

    monkeypatch.setattr(email_verification, "request_email_verification", forbidden)
    monkeypatch.setattr(profile, "update_customer_profile", forbidden)
    request = Request(
        {"type": "http", "method": "POST", "path": "/profile", "headers": []}
    )
    if adapter == "api":
        with pytest.raises(HTTPException) as denied:
            api.resend_verification_email_endpoint(
                auth={
                    "principal_id": str(target),
                    "principal_type": "subscriber",
                    "impersonated_by": str(uuid4()),
                },
                db=None,
            )
        assert denied.value.status_code == 403
    elif adapter == "reseller":
        monkeypatch.setattr(
            web_reseller_routes,
            "_require_reseller_context",
            lambda *args: {"is_impersonation": True},
        )
        assert (
            web_reseller_routes.reseller_resend_email_verification(
                request, None
            ).status_code
            == 403
        )
    else:
        monkeypatch.setattr(
            routes,
            "get_current_customer_from_request",
            lambda *args: {"id": str(target), "is_impersonation": True},
        )
        monkeypatch.setattr(routes, "_is_read_only_customer", lambda *args: False)
        response = (
            routes.customer_resend_email_verification(request, None)
            if adapter == "customer"
            else routes.customer_update_profile(
                request,
                first_name="Customer",
                last_name="Target",
                email="target@example.com",
                db=None,
            )
        )
        assert response.status_code == 403


@pytest.mark.parametrize("adapter", ["api", "customer", "reseller"])
def test_resend_adapter_maps_lock_timeout_to_retryable_503(monkeypatch, adapter):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from fastapi import HTTPException
    from starlette.requests import Request

    from app.api import auth_flow as api
    from app.services import rate_limiter_adapter, web_reseller_routes
    from app.services.db_session_adapter import db_session_adapter
    from app.web.customer import routes

    target = uuid4()

    @contextmanager
    def independent():
        yield object()

    def unavailable(*args, **kwargs):
        raise email_verification.EmailVerificationError(
            code="auth.email_verification.lock_timeout",
            message="Please try again.",
            retryable=True,
        )

    monkeypatch.setattr(db_session_adapter, "owner_command_session", independent)
    monkeypatch.setattr(email_verification, "request_email_verification", unavailable)
    request = Request(
        {"type": "http", "method": "POST", "path": "/profile", "headers": []}
    )
    if adapter == "api":
        monkeypatch.setattr(
            rate_limiter_adapter,
            "allow_operation",
            lambda *args, **kwargs: SimpleNamespace(allowed=True),
        )
        with pytest.raises(HTTPException) as denied:
            api.resend_verification_email_endpoint(
                auth={"principal_id": str(target), "principal_type": "subscriber"},
                db=None,
            )
        response = denied.value
    elif adapter == "customer":
        monkeypatch.setattr(
            routes,
            "get_current_customer_from_request",
            lambda *args: {"id": str(target)},
        )
        monkeypatch.setattr(routes, "_is_read_only_customer", lambda *args: False)
        monkeypatch.setattr(
            routes, "optional_customer_subscriber_id", lambda *args: target
        )
        response = routes.customer_resend_email_verification(request, None)
    else:
        monkeypatch.setattr(
            web_reseller_routes,
            "_require_reseller_context",
            lambda *args: {"subscriber": SimpleNamespace(id=target)},
        )
        response = web_reseller_routes.reseller_resend_email_verification(request, None)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"


def test_profile_and_verification_audit_share_explicit_actual_actor(
    db_session, subscriber, monkeypatch
):
    from app.services import customer_portal_profile_commands as profile

    actual_actor_id = uuid4()
    command = profile.UpdateCustomerProfileCommand(
        context=CommandContext.system(
            actor=str(actual_actor_id),
            scope=profile.PORTAL_PROFILE_WRITE_SCOPE,
            reason="Verify explicit actor propagation",
        ),
        actor=AuditActor.user(str(actual_actor_id)),
        subscriber_id=subscriber.id,
        first_name=subscriber.first_name,
        last_name=subscriber.last_name,
        email="actual-actor@example.com",
        billing_notifications=True,
        sms_updates=True,
    )
    monkeypatch.setattr(
        "app.services.customer_location_requests.geocode_service_address",
        lambda *args, **kwargs: None,
    )
    db_session.commit()
    profile.update_customer_profile(db_session, command=command)
    rows = db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.action.in_(
                ["portal_profile_update", "auth.email_verification_requested"]
            )
        )
    ).all()
    assert len(rows) == 2
    assert all(row.actor_id == str(actual_actor_id) for row in rows)
    assert all(row.entity_id == str(command.subscriber_id) for row in rows)


def test_competing_planner_replay_is_fingerprint_checked_and_rolled_back(
    db_session, subscriber, monkeypatch
):
    """Fast interleaving model; actual concurrency remains a PostgreSQL gate."""
    from dataclasses import replace

    command = _command(db_session, subscriber)
    original = email_verification.communication_intents.submit

    def competing_replay(db, intent):
        metadata = dict(intent.metadata)
        metadata["request_fingerprint"] = "different-committed-request-fingerprint"
        # Model a competing commit discovered by the planner's independent
        # dedupe read after the request owner's initial key lookup.
        original(db, replace(intent, metadata=metadata))
        result = original(db, intent)
        assert result.replayed
        return result

    monkeypatch.setattr(
        email_verification.communication_intents, "submit", competing_replay
    )
    with pytest.raises(
        email_verification.EmailVerificationError, match="different verification"
    ):
        email_verification.request_email_verification(db_session, command=command)
    assert db_session.scalars(select(CommunicationIntentRecord)).all() == []
    assert db_session.scalars(select(Notification)).all() == []
    assert (
        db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "auth.email_verification_requested"
            )
        ).all()
        == []
    )
