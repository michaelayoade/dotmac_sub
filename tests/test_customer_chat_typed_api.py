"""HTTP adapters preserve chat JSON while carrying authenticated typed commands."""

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.me import router
from app.db import get_db
from app.services import team_inbox_realtime
from app.services.auth_dependencies import require_user_auth
from tests.test_customer_field_chat_admission import _inbound_count, _visit


def _client(db_session, subscriber_id, *, principal_type="subscriber"):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: {
        "principal_type": principal_type,
        "subscriber_id": str(subscriber_id),
    }
    return TestClient(app)


def test_request_uuid_replays_typed_send_and_thread_keeps_transport_json(
    db_session, monkeypatch
):
    command, _, _, conversation_id = _visit(db_session)
    published = []
    monkeypatch.setattr(
        team_inbox_realtime,
        "publish_conversation_event",
        lambda *a, **k: published.append(k),
    )
    client = _client(db_session, command.subscriber_id)
    path = f"/me/work-orders/{command.work_order_public_id}/chat"
    payload = {"body": command.body, "request_id": str(command.context.command_id)}
    first = client.post(
        f"{path}/messages", json=payload, headers={"Authorization": "Bearer test"}
    )
    second = client.post(
        f"{path}/messages", json=payload, headers={"Authorization": "Bearer test"}
    )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert set(first.json()) == {
        "id",
        "body",
        "from_customer",
        "author_name",
        "created_at",
    }
    assert _inbound_count(db_session, conversation_id) == 1
    assert len(published) == 1
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()["messages"] == [first.json()]
    assert response.json()["conversation_id"] == str(conversation_id)
    assert response.json()["can_send"] is True
    changed = client.post(
        f"{path}/messages",
        json={**payload, "body": "Changed"},
        headers={"Authorization": "Bearer test"},
    )
    assert changed.status_code == 409


def test_adapter_derives_subscriber_from_auth_and_accepts_legacy_body(db_session):
    command, _, _, _ = _visit(db_session)
    path = f"/me/work-orders/{command.work_order_public_id}/chat"
    unrelated = _client(db_session, uuid4())
    response = unrelated.post(
        f"{path}/messages",
        json={"body": "No access", "subscriber_id": str(command.subscriber_id)},
        headers={"Authorization": "Bearer test"},
    )
    assert response.status_code == 404
    assert unrelated.get(path).json()["reason"] == "not_found"
    staff = _client(db_session, command.subscriber_id, principal_type="system_user")
    assert staff.get(path).status_code == 403
    assert (
        staff.post(
            f"{path}/messages",
            json={"body": "No access"},
            headers={"Authorization": "Bearer test"},
        ).status_code
        == 403
    )
    owner = _client(db_session, command.subscriber_id)
    assert (
        owner.post(
            f"{path}/messages",
            json={"body": "Legacy send"},
            headers={"Authorization": "Bearer test"},
        ).status_code
        == 201
    )
    assert (
        owner.post(
            f"{path}/messages",
            json={"body": "Malformed", "request_id": "invalid"},
            headers={"Authorization": "Bearer test"},
        ).status_code
        == 422
    )
