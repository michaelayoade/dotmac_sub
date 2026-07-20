"""CRM inbound identity, replay, and consequence guarantees."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import uuid
from unittest.mock import patch

import pytest

from app.api.crm_webhooks import receive_crm_chat_event
from app.models.integration_platform import IntegrationInbox
from app.models.subscriber import Subscriber
from app.services.integrations.inbox import InboxError
from tests.integration_platform_helpers import enable_crm_inbound

SECRET = "test-webhook-secret"


@pytest.fixture(autouse=True)
def _crm_inbound_installation(db_session, monkeypatch):
    return enable_crm_inbound(
        db_session,
        monkeypatch,
        signing_secret=SECRET,
    )


class _FakeRequest:
    def __init__(self, raw: bytes, headers: dict[str, str]):
        self._raw = raw
        self.headers = headers

    async def body(self) -> bytes:
        return self._raw

    async def json(self):
        return json.loads(self._raw)


def _run(coro):
    box: dict[str, object] = {}

    def runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["result"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]


def _request(body: dict, event: str, *, delivery_id: str | None = None):
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    headers = {
        "X-Webhook-Event": event,
        "X-Webhook-Signature-256": signature,
        "Content-Type": "application/json",
    }
    if delivery_id:
        headers["X-Webhook-Delivery-Id"] = delivery_id
    return _FakeRequest(raw, headers)


def _linked_subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="CRM",
        last_name="Inbound",
        email=f"crm-inbox-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()
    return subscriber


def test_chat_redelivery_returns_stored_consequence_without_double_push(db_session):
    delivery_id = str(uuid.uuid4())
    body = {
        "subscriber_id": str(uuid.uuid4()),
        "preview": "hi",
        "conversation_id": "c1",
    }
    with patch("app.services.push.send_push") as send_push:
        first = _run(
            receive_crm_chat_event(
                _request(body, "message.outbound", delivery_id=delivery_id),
                db_session,
            )
        )
        replay = _run(
            receive_crm_chat_event(
                _request(body, "message.outbound", delivery_id=delivery_id),
                db_session,
            )
        )

    assert first == replay == {"status": "ok", "event": "message.outbound"}
    assert send_push.call_count == 1
    assert db_session.query(IntegrationInbox).count() == 1


def test_provider_identity_collision_quarantines_installation(
    db_session,
    _crm_inbound_installation,
):
    delivery_id = str(uuid.uuid4())
    first = _request(
        {"subscriber_id": str(uuid.uuid4()), "preview": "first"},
        "message.outbound",
        delivery_id=delivery_id,
    )
    second = _request(
        {"subscriber_id": str(uuid.uuid4()), "preview": "changed"},
        "message.outbound",
        delivery_id=delivery_id,
    )
    with patch("app.services.push.send_push"):
        _run(receive_crm_chat_event(first, db_session))
        with pytest.raises(InboxError, match="identity collision"):
            _run(receive_crm_chat_event(second, db_session))

    db_session.refresh(_crm_inbound_installation.installation)
    assert _crm_inbound_installation.installation.state == "quarantined"
