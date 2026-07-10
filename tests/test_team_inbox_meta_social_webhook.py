from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import meta_inbox_webhooks
from app.models.team_inbox import InboxChannelType, InboxConversation, InboxMessage

META_TEST_SECRET = "meta-secret"  # pragma: allowlist secret


def _request(body: bytes, headers: dict[str, str] | None = None) -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/webhooks/meta",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ],
    }
    return Request(scope, receive)


def _sign(body: bytes, secret: str = META_TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_meta_inbox_webhook_verify_returns_challenge(db_session, monkeypatch):
    monkeypatch.setattr(meta_inbox_webhooks, "_verify_token", lambda db: "verify-token")

    response = meta_inbox_webhooks.verify_meta_inbox_webhook(
        mode="subscribe",
        token="verify-token",
        challenge="challenge-123",
        db=db_session,
    )

    assert response.body == b"challenge-123"


@pytest.mark.asyncio
async def test_meta_inbox_webhook_rejects_bad_signature(db_session, monkeypatch):
    body = b'{"entry":[]}'
    request = _request(body, {"X-Hub-Signature-256": "sha256=bad"})

    monkeypatch.setattr(
        meta_inbox_webhooks,
        "_verify_meta_signature",
        lambda db, body, sig: (_ for _ in ()).throw(
            HTTPException(status_code=401, detail="bad")
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_meta_inbox_webhook_creates_facebook_messenger_message(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        meta_inbox_webhooks, "_verify_meta_signature", lambda db, body, sig: None
    )
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "page-1",
                "messaging": [
                    {
                        "sender": {"id": "123456789012345"},
                        "recipient": {"id": "page-1"},
                        "timestamp": 1783670400000,
                        "message": {
                            "mid": "m_fb_1",
                            "text": "Hello support",
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    response = await meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session)

    conversation = db_session.query(InboxConversation).one()
    message = db_session.query(InboxMessage).one()
    assert response["status"] == "ok"
    assert response["processed"] == 1
    assert response["items"][0]["resolution_status"] == "unmatched"
    assert conversation.channel_type == InboxChannelType.facebook_messenger.value
    assert conversation.contact_address == "123456789012345"
    assert conversation.external_thread_id == "facebook_messenger:123456789012345"
    assert message.external_message_id == "m_fb_1"
    assert message.from_address == "123456789012345"
    assert message.body == "Hello support"
    assert message.metadata_["provider"] == "meta"
    assert message.metadata_["platform"] == InboxChannelType.facebook_messenger.value


@pytest.mark.asyncio
async def test_meta_inbox_webhook_creates_instagram_dm_message(db_session, monkeypatch):
    monkeypatch.setattr(
        meta_inbox_webhooks, "_verify_meta_signature", lambda db, body, sig: None
    )
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-1",
                "messaging": [
                    {
                        "sender": {"id": "17841400000000000"},
                        "recipient": {"id": "ig-1"},
                        "timestamp": 1783670500000,
                        "message": {
                            "mid": "m_ig_1",
                            "text": "Please check my account",
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    response = await meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session)

    conversation = db_session.query(InboxConversation).one()
    message = db_session.query(InboxMessage).one()
    assert response["processed"] == 1
    assert conversation.channel_type == InboxChannelType.instagram_dm.value
    assert conversation.contact_address == "17841400000000000"
    assert conversation.external_thread_id == "instagram_dm:17841400000000000"
    assert message.external_message_id == "m_ig_1"
    assert message.body == "Please check my account"


@pytest.mark.asyncio
async def test_meta_inbox_webhook_deduplicates_external_message_id(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        meta_inbox_webhooks, "_verify_meta_signature", lambda db, body, sig: None
    )
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "page-1",
                "messaging": [
                    {
                        "sender": {"id": "psid-1"},
                        "timestamp": 1783670400000,
                        "message": {"mid": "m_dup", "text": "Hello"},
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})
    first = await meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session)
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    second = await meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session)

    assert first["items"][0]["kind"] == "received"
    assert second["items"][0]["kind"] == "duplicate"
    assert db_session.query(InboxConversation).count() == 1
    assert db_session.query(InboxMessage).count() == 1


@pytest.mark.asyncio
async def test_meta_inbox_webhook_preserves_attachment_messages(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        meta_inbox_webhooks, "_verify_meta_signature", lambda db, body, sig: None
    )
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "page-1",
                "messaging": [
                    {
                        "sender": {"id": "psid-1"},
                        "timestamp": 1783670400000,
                        "message": {
                            "mid": "m_img",
                            "attachments": [
                                {
                                    "type": "image",
                                    "payload": {"url": "https://example.test/i.jpg"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    response = await meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session)

    message = db_session.query(InboxMessage).one()
    assert response["processed"] == 1
    assert message.body == "[image]"
    assert message.metadata_["attachments"][0]["type"] == "image"
    assert message.metadata_["attachments"][0]["url"] == "https://example.test/i.jpg"
    assert message.metadata_["raw"]["message"]["attachments"][0]["type"] == "image"
