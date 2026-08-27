"""Mobile push: device-token registry + config-gated FCM transport."""

import json
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api import me as me_api
from app.models.device_token import DeviceToken
from app.models.notification import Notification, NotificationChannel
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.schemas.notification import (
    PUSH_INTENT_REGISTRY,
    PushIntent,
    PushTokenRegister,
)
from app.services import push as push_service


def _principal(subscriber):
    return {
        "principal_type": "subscriber",
        "subscriber_id": str(subscriber.id),
    }


def test_register_upserts_and_lists_active(db_session, subscriber):
    push_service.register_token(db_session, str(subscriber.id), "tok-1", "android")
    assert push_service.active_tokens(db_session, str(subscriber.id)) == ["tok-1"]

    # Re-registering the same token updates in place (no duplicate row).
    push_service.register_token(db_session, str(subscriber.id), "tok-1", "ios")
    rows = db_session.query(DeviceToken).filter(DeviceToken.token == "tok-1").all()
    assert len(rows) == 1
    assert rows[0].platform == "ios"
    assert rows[0].system_user_id is None


def test_system_user_token_moves_existing_token_from_subscriber(db_session, subscriber):
    user = SystemUser(
        first_name="Ade",
        last_name="Tech",
        email=f"ade-{uuid.uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.commit()
    push_service.register_token(db_session, str(subscriber.id), "tok-shared", "android")

    row = push_service.register_system_user_token(
        db_session,
        str(user.id),
        "tok-shared",
        platform="ios",
        app_version="1.2.0",
    )

    assert row.subscriber_id is None
    assert row.system_user_id == user.id
    assert row.platform == "ios"
    assert row.app_version == "1.2.0"
    assert push_service.active_tokens(db_session, str(subscriber.id)) == []
    assert push_service.active_system_user_tokens(db_session, str(user.id)) == [
        "tok-shared"
    ]


def test_unregister_deactivates_and_is_idempotent(db_session, subscriber):
    push_service.register_token(db_session, str(subscriber.id), "tok-2", "android")
    assert (
        push_service.unregister_token(db_session, str(subscriber.id), "tok-2") is True
    )
    assert push_service.active_tokens(db_session, str(subscriber.id)) == []
    # Unknown token → no-op False, no raise.
    assert (
        push_service.unregister_token(db_session, str(subscriber.id), "nope") is False
    )


def test_send_push_noop_without_tokens(db_session, subscriber):
    # Nothing registered → success (nothing to deliver), no transport attempted.
    assert push_service.send_push(db_session, str(subscriber.id), "T", "B") is True


def test_send_push_noop_when_fcm_unconfigured(db_session, subscriber, monkeypatch):
    monkeypatch.delenv("FCM_PROJECT_ID", raising=False)
    monkeypatch.delenv("FCM_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("FCM transport attempted while unconfigured")

    monkeypatch.setattr(push_service.httpx, "post", _boom)

    push_service.register_token(db_session, str(subscriber.id), "tok-3", "android")
    # Token present but FCM not configured → safe no-op success, no HTTP call.
    assert push_service.send_push(db_session, str(subscriber.id), "T", "B") is True


def test_fcm_transport_sends_only_generic_display_and_push_intent_v1(
    db_session, subscriber, monkeypatch
):
    push_service.register_token(db_session, str(subscriber.id), "tok-safe", "android")
    monkeypatch.setattr(
        push_service,
        "_fcm_config",
        lambda: {"project_id": "test-project"},
    )
    monkeypatch.setattr(push_service, "_access_token", lambda _: "access-token")
    requests = []

    def fake_post(url, *, headers, json, timeout):
        requests.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(push_service.httpx, "post", fake_post)
    customer_content = "Customer ACME supplied account-specific diagnostic details"

    assert push_service.send_push(
        db_session,
        str(subscriber.id),
        title="Work order for ACME",
        body=customer_content,
        intent=PushIntent(
            intent_code="ticket.closed",
            subject_kind="ticket",
            subject_id="3f1c2b4a-5d6e-4f70-8a91-b2c3d4e5f607",
        ),
        notification_id=str(uuid.uuid4()),
    )

    assert len(requests) == 1
    message = requests[0]["json"]["message"]
    assert message["notification"] == {
        "title": "Dotmac update",
        "body": "Open the app to view your update.",
    }
    assert message["data"]["contract_version"] == "PushIntentV1"
    assert message["data"]["intent_code"] == "ticket.closed"
    assert message["data"]["subject_kind"] == "ticket"
    assert message["data"]["subject_id"] == ("3f1c2b4a-5d6e-4f70-8a91-b2c3d4e5f607")
    assert message["data"]["tenant_id"]
    assert message["data"]["principal_id"] == str(subscriber.id)
    assert message["data"]["issued_at"].endswith("Z")
    assert set(message["data"]) == {
        "contract_version",
        "intent_code",
        "subject_kind",
        "subject_id",
        "tenant_id",
        "principal_id",
        "issued_at",
    }
    serialized = json.dumps(requests[0]["json"])
    assert customer_content not in serialized
    assert "Work order for ACME" not in serialized


def test_rich_content_stays_in_authenticated_notification_record(
    db_session, subscriber
):
    intent = PushIntent(
        intent_code="chat.message",
        subject_kind="conversation",
        subject_id="conversation-42",
    )

    assert push_service.send_push(
        db_session,
        str(subscriber.id),
        title="New message from support",
        body="Private account-specific reply",
        intent=intent,
    )

    notification = (
        db_session.query(Notification)
        .filter(Notification.channel == NotificationChannel.push)
        .order_by(Notification.created_at.desc())
        .first()
    )
    assert notification.subject == "New message from support"
    assert notification.body == "Private account-specific reply"
    assert push_service.intent_for_notification(notification) == intent


def test_staff_fcm_transport_uses_the_same_content_free_boundary(
    db_session, monkeypatch
):
    user = SystemUser(
        first_name="Field",
        last_name="Operator",
        email=f"field-{uuid.uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.commit()
    push_service.register_system_user_token(
        db_session, str(user.id), "tok-staff", "android"
    )
    monkeypatch.setattr(
        push_service,
        "_fcm_config",
        lambda: {"project_id": "test-project"},
    )
    monkeypatch.setattr(push_service, "_access_token", lambda _: "access-token")
    payloads = []
    monkeypatch.setattr(
        push_service.httpx,
        "post",
        lambda _url, *, headers, json, timeout: (
            payloads.append(json) or SimpleNamespace(status_code=200)
        ),
    )

    assert push_service.send_push_to_system_user(
        db_session,
        str(user.id),
        title="OUTAGE ESCALATION: Customer ACME",
        body="Customer says their private circuit is down",
        intent=PushIntent(
            intent_code="operational.escalation",
            subject_kind="operational_escalation",
            subject_id="delivery-1",
        ),
        notification_id="delivery-1",
    )

    serialized = json.dumps(payloads)
    assert "Customer ACME" not in serialized
    assert "private circuit" not in serialized
    assert payloads[0]["message"]["data"]["intent_code"] == ("operational.escalation")


def test_push_intent_rejects_undeclared_and_content_fields():
    with pytest.raises(ValidationError, match="undeclared push intent code"):
        PushIntent(
            intent_code="provider.route",
            subject_kind="notification",
            subject_id="one",
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PushIntent(
            intent_code="notification.open",
            subject_kind="notification",
            subject_id="one",
            body="customer content",
        )


_PUSH_WIRE_KEYS = {
    "contract_version",
    "intent_code",
    "subject_kind",
    "subject_id",
    "tenant_id",
    "principal_id",
    "issued_at",
}

# Names a transport payload must never be able to reintroduce. Kept explicit so
# the sweep below fails loudly if a future field smuggles content or a location
# back onto the wire instead of silently widening the contract.
_FORBIDDEN_WIRE_KEYS = {
    "body",
    "comment",
    "deep_link",
    "description",
    "link",
    "message",
    "path",
    "preview",
    "route",
    "subject",
    "title",
    "token",
    "url",
    "work_order_description",
}


def test_every_declared_intent_code_emits_the_same_closed_wire_shape():
    """Non-vacuity: the boundary is proven for the whole registry, not one code.

    A single happy-path assertion would still pass if a new intent code carried
    extra fields, so every declared code is driven through the real payload
    builder here.
    """
    assert PUSH_INTENT_REGISTRY, "the intent registry must not be empty"
    for intent_code, subject_kind in PUSH_INTENT_REGISTRY.items():
        payload = push_service._fcm_payload(
            token="tok-sweep",
            intent=push_service._wire_intent(
                PushIntent(
                    intent_code=intent_code,
                    subject_kind=subject_kind,
                    subject_id="subject-1",
                ),
                principal_id="principal-1",
            ),
        )
        message = payload["message"]
        assert message["notification"] == {
            "title": "Dotmac update",
            "body": "Open the app to view your update.",
        }, intent_code
        assert set(message["data"]) == _PUSH_WIRE_KEYS, intent_code
        assert not _FORBIDDEN_WIRE_KEYS & set(message["data"]), intent_code
        assert all(isinstance(value, str) for value in message["data"].values())


def test_register_endpoint_creates_row(db_session, subscriber):
    out = me_api.my_register_push_token(
        payload=PushTokenRegister(token="endpoint-tok", platform="android"),
        db=db_session,
        principal=_principal(subscriber),
    )
    assert out.platform == "android"
    assert out.is_active is True
    assert push_service.active_tokens(db_session, str(subscriber.id)) == [
        "endpoint-tok"
    ]


def test_unregister_endpoint_is_idempotent(db_session, subscriber):
    push_service.register_token(db_session, str(subscriber.id), "ep-tok", "ios")
    me_api.my_unregister_push_token(
        token="ep-tok", db=db_session, principal=_principal(subscriber)
    )
    assert push_service.active_tokens(db_session, str(subscriber.id)) == []
    # Deleting an unknown token must not raise.
    me_api.my_unregister_push_token(
        token=str(uuid.uuid4()), db=db_session, principal=_principal(subscriber)
    )
