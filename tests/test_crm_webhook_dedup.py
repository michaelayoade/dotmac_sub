"""CRM inbound identity, replay, and consequence guarantees.

These properties belong to the shared inbound envelope
(`integration_inbox.receive_and_claim_verified` / `complete_consequence`), not
to any one receiver. They were originally driven through the chat receiver,
which was removed on 2026-08-30 with ADR 0006, so they now drive the surviving
`POST /webhooks/crm` receiver instead. An event outside `TICKET_EVENTS` is used
deliberately: it exercises the claim/store/replay path with a consequence that
depends on nothing else, which is the property under test.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import uuid

import pytest

from app.api.crm_webhooks import receive_crm_event
from app.models.integration_platform import IntegrationInbox
from app.services.integrations.inbox import InboxError
from tests.integration_platform_helpers import enable_crm_inbound

SECRET = "test-webhook-secret"

#: Valid, signed, and deliberately outside `TICKET_EVENTS`, so the receiver
#: stores an `ignored` consequence and the replay assertions are about the
#: envelope rather than a domain consequence.
INERT_EVENT = "ticket.commented"


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


def test_redelivery_returns_the_stored_consequence_exactly_once(db_session):
    delivery_id = str(uuid.uuid4())
    body = {"ticket_id": str(uuid.uuid4())}

    first = _run(
        receive_crm_event(
            _request(body, INERT_EVENT, delivery_id=delivery_id),
            db_session,
        )
    )
    replay = _run(
        receive_crm_event(
            _request(body, INERT_EVENT, delivery_id=delivery_id),
            db_session,
        )
    )

    assert first == replay == {"status": "ignored", "event": INERT_EVENT}
    assert db_session.query(IntegrationInbox).count() == 1


def test_provider_identity_collision_quarantines_installation(
    db_session,
    _crm_inbound_installation,
):
    delivery_id = str(uuid.uuid4())
    first = _request({"ticket_id": "first"}, INERT_EVENT, delivery_id=delivery_id)
    second = _request({"ticket_id": "changed"}, INERT_EVENT, delivery_id=delivery_id)

    _run(receive_crm_event(first, db_session))
    with pytest.raises(InboxError, match="identity collision"):
        _run(receive_crm_event(second, db_session))

    db_session.refresh(_crm_inbound_installation.installation)
    assert _crm_inbound_installation.installation.state == "quarantined"
