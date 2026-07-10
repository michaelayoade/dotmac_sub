from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.field_chat import FieldJobChatMessage
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.services.auth_dependencies import require_user_auth
from app.services.field.chat import field_job_chat


def _user(db_session, name: str = "Chat") -> SystemUser:
    user = SystemUser(
        first_name=name,
        last_name="Tech",
        display_name=f"{name} Tech",
        email=f"{name.lower()}-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _auth(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _profile(
    db_session, user: SystemUser, crm_person_id: str = "crm-chat-tech"
) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
        title="Installer",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Chat",
        last_name="Customer",
        email=f"chat-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrderMirror:
    row = WorkOrderMirror(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-chat"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Chat job"),
        status=overrides.pop("status", "in_progress"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-chat-tech"
        ),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_chat_thread_send_and_ordering(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-chat-flow")
    db_session.commit()

    thread = field_job_chat.get_thread(db_session, _auth(user), "wo-chat-flow")
    assert thread["available"] is True
    assert thread["can_send"] is True
    assert thread["customer_name"] == "Chat Customer"
    assert thread["messages"] == []

    first = field_job_chat.send_message(
        db_session, _auth(user), "wo-chat-flow", body="  On my way  "
    )
    assert first["body"] == "On my way"
    assert first["direction"] == "staff"
    assert first["author_name"] == "Chat Tech"

    field_job_chat.send_message(
        db_session, _auth(user), "wo-chat-flow", body="Arrived on site"
    )
    thread = field_job_chat.get_thread(db_session, _auth(user), "wo-chat-flow")
    assert [message["body"] for message in thread["messages"]] == [
        "On my way",
        "Arrived on site",
    ]
    assert thread["conversation_id"] is not None
    stored = (
        db_session.query(FieldJobChatMessage)
        .filter(FieldJobChatMessage.crm_work_order_id == "wo-chat-flow")
        .count()
    )
    assert stored == 2


def test_chat_send_blocked_for_terminal_job_state(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-chat-done",
        status="completed",
    )
    db_session.commit()

    thread = field_job_chat.get_thread(db_session, _auth(user), "wo-chat-done")
    assert thread["available"] is True
    assert thread["can_send"] is False

    with pytest.raises(HTTPException) as exc:
        field_job_chat.send_message(
            db_session, _auth(user), "wo-chat-done", body="Too late"
        )
    assert exc.value.status_code == 409


def test_chat_scoped_to_assigned_technician(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    other = _user(db_session, "Other")
    _profile(db_session, other, crm_person_id="other-chat-tech")
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-chat-hidden",
        assigned_to_crm_person_id="other-chat-tech",
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_job_chat.get_thread(db_session, _auth(user), "wo-chat-hidden")
    assert exc.value.status_code == 404


def test_chat_api(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-chat-api")
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    empty = client.get("/api/v1/field/jobs/wo-chat-api/chat")
    assert empty.status_code == 200
    assert empty.json()["available"] is True
    assert empty.json()["messages"] == []

    sent = client.post(
        "/api/v1/field/jobs/wo-chat-api/chat/messages",
        json={"body": "Hello from the field"},
    )
    assert sent.status_code == 201
    assert sent.json()["direction"] == "staff"
    assert sent.json()["body"] == "Hello from the field"

    thread = client.get("/api/v1/field/jobs/wo-chat-api/chat")
    assert thread.status_code == 200
    assert len(thread.json()["messages"]) == 1
    assert thread.json()["messages"][0]["author_name"] == "Chat Tech"

    blank = client.post(
        "/api/v1/field/jobs/wo-chat-api/chat/messages", json={"body": "   "}
    )
    assert blank.status_code == 422
