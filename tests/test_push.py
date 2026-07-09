"""Mobile push: device-token registry + config-gated FCM transport."""

import uuid

from app.api import me as me_api
from app.models.device_token import DeviceToken
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.schemas.notification import PushTokenRegister
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
