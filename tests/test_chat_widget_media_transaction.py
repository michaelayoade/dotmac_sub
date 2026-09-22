"""Visitor message adapters enter the owner command on a clean session."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.datastructures import Headers
from starlette.requests import Request

from app.api import chat_widget
from app.services import team_inbox_widget


def test_media_message_finishes_token_read_before_owner_command(monkeypatch) -> None:
    conversation_id = uuid4()
    principal = team_inbox_widget.WidgetPrincipal(
        conversation_id=conversation_id,
        session_id="visitor-session",
        surface="customer",
    )

    def decode(db: Session, token: str) -> team_inbox_widget.WidgetPrincipal:
        assert token == "visitor-token"
        db.execute(select(1))
        assert db.in_transaction()
        return principal

    def add_message(
        db: Session,
        *,
        session_id: str,
        principal: team_inbox_widget.WidgetPrincipal,
        command: team_inbox_widget.VisitorMessageCommand,
    ) -> team_inbox_widget.VisitorMessageOutcome:
        assert not db.in_transaction()
        assert session_id == "visitor-session"
        assert principal.conversation_id == conversation_id
        assert command.client_message_id == "photo-1"
        assert command.photos == (
            team_inbox_widget.VisitorPhoto(
                file_name="router.jpg",
                content_type="image/jpeg",
                data=b"photo-bytes",
            ),
        )
        return team_inbox_widget.VisitorMessageOutcome(
            message_id=uuid4(),
            conversation_id=conversation_id,
            body=command.body,
            created_at=datetime.now(UTC),
            client_message_id=command.client_message_id,
            attachments=(
                team_inbox_widget.VisitorAttachment(
                    asset_id=uuid4(), file_name="router.jpg"
                ),
            ),
        )

    monkeypatch.setattr(chat_widget.team_inbox_widget, "decode_widget_token", decode)
    monkeypatch.setattr(chat_widget, "_require_message_rate", lambda *_: None)
    monkeypatch.setattr(
        chat_widget.team_inbox_widget, "add_visitor_message_committed", add_message
    )
    request = Request({"type": "http", "method": "POST", "headers": []})
    upload = UploadFile(
        file=BytesIO(b"photo-bytes"),
        filename="router.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )

    with Session(create_engine("sqlite+pysqlite:///:memory:")) as db:
        result = asyncio.run(
            chat_widget.widget_session_media_message_create(
                session_id="visitor-session",
                request=request,
                body="",
                client_message_id="photo-1",
                attachments=[upload],
                x_visitor_token="visitor-token",
                db=db,
            )
        )

    assert result["client_message_id"] == "photo-1"
    assert result["attachments"][0]["file_name"] == "router.jpg"


def test_media_route_accepts_mobile_multipart_fields(monkeypatch) -> None:
    principal = team_inbox_widget.WidgetPrincipal(
        conversation_id=uuid4(),
        session_id="visitor-session",
        surface="customer",
    )
    monkeypatch.setattr(chat_widget, "_principal", lambda *_: principal)
    monkeypatch.setattr(chat_widget, "_require_message_rate", lambda *_: None)

    def add_message(
        db: Session,
        *,
        session_id: str,
        principal: team_inbox_widget.WidgetPrincipal,
        command: team_inbox_widget.VisitorMessageCommand,
    ) -> team_inbox_widget.VisitorMessageOutcome:
        assert session_id == principal.session_id
        assert command.body == "My router"
        assert command.client_message_id == "photo-2"
        assert command.photos == (
            team_inbox_widget.VisitorPhoto(
                file_name="router.jpg",
                content_type="image/jpeg",
                data=b"photo-bytes",
            ),
        )
        return team_inbox_widget.VisitorMessageOutcome(
            message_id=uuid4(),
            conversation_id=principal.conversation_id,
            body=command.body,
            created_at=datetime.now(UTC),
            client_message_id=command.client_message_id,
            attachments=(),
        )

    monkeypatch.setattr(
        chat_widget.team_inbox_widget, "add_visitor_message_committed", add_message
    )
    app = FastAPI()
    app.include_router(chat_widget.router)
    with Session(create_engine("sqlite+pysqlite:///:memory:")) as db:
        app.dependency_overrides[chat_widget.get_db] = lambda: db
        response = TestClient(app).post(
            "/widget/session/visitor-session/message/media",
            headers={"X-Visitor-Token": "visitor-token"},
            data={"body": "My router", "client_message_id": "photo-2"},
            files=[("attachments", ("router.jpg", b"photo-bytes", "image/jpeg"))],
        )

    assert response.status_code == 200
    assert response.json()["client_message_id"] == "photo-2"
