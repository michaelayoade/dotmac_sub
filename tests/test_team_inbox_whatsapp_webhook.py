from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import inbox_webhooks
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.team_inbox import InboxConversation, InboxMessage, InboxMessageDirection

META_TEST_SECRET = "meta-secret"  # pragma: allowlist secret


def _run_async(coro):
    # Run the webhook coroutine on its own loop to avoid suite-level loop reuse.
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result()


def _request(body: bytes, headers: dict[str, str] | None = None) -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/webhooks/whatsapp/meta",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ],
    }
    return Request(scope, receive)


def _sign(body: bytes, secret: str = META_TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Nwosu",
        email="ada@example.com",
        phone="0803 555 0114",
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def test_meta_webhook_verify_returns_challenge(db_session, monkeypatch):
    monkeypatch.setattr(inbox_webhooks, "_verify_token", lambda db: "verify-token")

    response = inbox_webhooks.verify_meta_webhook(
        mode="subscribe",
        token="verify-token",
        challenge="challenge-123",
        db=db_session,
    )

    assert response.body == b"challenge-123"


def test_meta_webhook_verify_rejects_bad_token(db_session, monkeypatch):
    monkeypatch.setattr(inbox_webhooks, "_verify_token", lambda db: "verify-token")

    with pytest.raises(HTTPException) as exc:
        inbox_webhooks.verify_meta_webhook(
            mode="subscribe",
            token="wrong",
            challenge="challenge-123",
            db=db_session,
        )

    assert exc.value.status_code == 403


def test_meta_whatsapp_webhook_rejects_bad_signature(db_session, monkeypatch):
    monkeypatch.setattr(inbox_webhooks, "_app_secret", lambda db: META_TEST_SECRET)
    body = b'{"entry":[]}'
    request = _request(body, {"X-Hub-Signature-256": "sha256=bad"})

    with pytest.raises(HTTPException) as exc:
        _run_async(inbox_webhooks.receive_meta_whatsapp_webhook(request, db_session))

    assert exc.value.status_code == 401


def test_meta_whatsapp_webhook_creates_native_inbox_message(db_session, monkeypatch):
    monkeypatch.setattr(inbox_webhooks, "_app_secret", lambda db: META_TEST_SECRET)
    subscriber = _subscriber(db_session)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "08000000000",
                                "phone_number_id": "phone-number-id",
                            },
                            "contacts": [
                                {
                                    "wa_id": "2348035550114",
                                    "profile": {"name": "Ada Nwosu"},
                                }
                            ],
                            "messages": [
                                {
                                    "from": "2348035550114",
                                    "id": "wamid.meta-1",
                                    "timestamp": "1783670400",
                                    "type": "text",
                                    "text": {"body": "My internet is down"},
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

    response = _run_async(
        inbox_webhooks.receive_meta_whatsapp_webhook(request, db_session)
    )

    conversation = db_session.query(InboxConversation).one()
    message = db_session.query(InboxMessage).one()
    assert response["status"] == "ok"
    assert response["processed"] == 1
    assert response["items"][0]["subscriber_id"] == str(subscriber.id)
    assert conversation.subscriber_id == subscriber.id
    assert conversation.contact_address == "+2348035550114"
    assert message.external_message_id == "wamid.meta-1"
    assert message.body == "My internet is down"


def test_meta_whatsapp_webhook_updates_outbound_delivery_status(
    db_session, monkeypatch
):
    monkeypatch.setattr(inbox_webhooks, "_app_secret", lambda db: META_TEST_SECRET)
    conversation = InboxConversation(
        channel_type="whatsapp",
        contact_address="+2348035550114",
        external_thread_id="whatsapp:+2348035550114",
    )
    db_session.add(conversation)
    db_session.flush()
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type="whatsapp",
        direction=InboxMessageDirection.outbound.value,
        body="We are checking this.",
        external_message_id="wamid.outbound-1",
        to_addresses=["+2348035550114"],
        metadata_={"provider_result": {"provider_message_id": "wamid.outbound-1"}},
    )
    db_session.add(message)
    db_session.commit()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [
                                {
                                    "id": "wamid.outbound-1",
                                    "status": "delivered",
                                    "timestamp": "1783670500",
                                    "recipient_id": "2348035550114",
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

    response = _run_async(
        inbox_webhooks.receive_meta_whatsapp_webhook(request, db_session)
    )

    db_session.refresh(message)
    assert response["processed"] == 0
    assert response["status_processed"] == 1
    assert response["status_items"][0]["kind"] == "updated"
    assert message.metadata_["delivery_status"] == "delivered"
    assert message.metadata_["delivery_status_at"] == "1783670500"
    assert message.metadata_["delivery_recipient_id"] == "2348035550114"
    assert message.metadata_["delivery_status_history"][-1]["status"] == "delivered"


def test_meta_whatsapp_webhook_acknowledges_unknown_status(db_session, monkeypatch):
    monkeypatch.setattr(inbox_webhooks, "_app_secret", lambda db: META_TEST_SECRET)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.missing",
                                    "status": "failed",
                                    "timestamp": "1783670600",
                                    "recipient_id": "2348035550114",
                                    "errors": [{"code": 131026}],
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    response = _run_async(
        inbox_webhooks.receive_meta_whatsapp_webhook(request, db_session)
    )

    assert response["processed"] == 0
    assert response["status_processed"] == 1
    assert response["status_items"][0] == {
        "kind": "not_found",
        "provider_message_id": "wamid.missing",
        "status": "failed",
    }
    assert db_session.query(InboxMessage).count() == 0
