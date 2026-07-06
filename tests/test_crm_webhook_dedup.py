"""S4: inbound CRM webhook delivery dedup (idempotency).

A byte-identical redelivery of a signed webhook must not re-run its side effect
(a duplicate chat/reward push, a re-fired lifecycle push, or a delta re-apply
that reverts a newer status). Covered here for the side-effecting handlers and,
since P-3, the project/work-order/quote mirror handlers that apply a delta +
fire a customer push (the ticket handler re-pulls authoritative state, so it
stays idempotent without a claim).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from app.api.crm_webhooks import (
    _delivery_uuid,
    receive_crm_chat_event,
    receive_crm_referral_event,
    receive_crm_work_order_event,
)
from app.config import settings
from app.models.crm_webhook_delivery import CrmWebhookDelivery
from app.models.referral import ReferralMirror
from app.models.subscriber import Subscriber
from app.models.work_order_mirror import WorkOrderMirror

SECRET = "test-webhook-secret"


@contextmanager
def _with_secret(value: str):
    original = settings.crm_webhook_secret
    object.__setattr__(settings, "crm_webhook_secret", value)
    try:
        yield
    finally:
        object.__setattr__(settings, "crm_webhook_secret", original)


class _FakeRequest:
    def __init__(self, raw: bytes, headers: dict[str, str]):
        self._raw = raw
        self.headers = headers

    async def body(self) -> bytes:
        return self._raw


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _run(coro):
    box: dict[str, object] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["result"] = loop.run_until_complete(coro)
        finally:
            loop.close()

    thread = threading.Thread(target=_runner)
    thread.start()
    thread.join()
    return box["result"]


def _headers(
    raw: bytes, event: str, *, delivery_id: str | None = None
) -> dict[str, str]:
    h = {
        "X-Webhook-Event": event,
        "Content-Type": "application/json",
        "X-Webhook-Signature-256": _sign(raw),
    }
    if delivery_id is not None:
        h["X-Webhook-Delivery-Id"] = delivery_id
    return h


def _linked_subscriber(db_session, crm_id: uuid.UUID) -> Subscriber:
    sub = Subscriber(
        first_name="Ref",
        last_name="Errer",
        email=f"r-{uuid.uuid4().hex[:8]}@example.com",
        crm_subscriber_id=crm_id,
    )
    db_session.add(sub)
    db_session.commit()
    return sub


# ── referral dedup ────────────────────────────────────────────────────────


def _post_referral(db_session, body: dict, event="referral.captured"):
    raw = json.dumps(body).encode()
    return _run(
        receive_crm_referral_event(_FakeRequest(raw, _headers(raw, event)), db_session)
    )


def test_referral_redelivery_is_deduped(db_session):
    crm_id = uuid.uuid4()
    _linked_subscriber(db_session, crm_id)
    body = {
        "crm_subscriber_id": str(crm_id),
        "referral_id": "r-1",
        "referred_name": "Ada",
    }

    with _with_secret(SECRET):
        first = _post_referral(db_session, body)
        second = _post_referral(db_session, body)

    assert first["status"] == "ok"
    assert second["status"] == "ignored" and second["reason"] == "duplicate"
    # The mirror service ran exactly once.
    assert (
        db_session.query(ReferralMirror).filter_by(crm_referral_id="r-1").count() == 1
    )
    assert db_session.query(CrmWebhookDelivery).count() == 1


def test_distinct_referral_events_both_process(db_session):
    crm_id = uuid.uuid4()
    _linked_subscriber(db_session, crm_id)

    with _with_secret(SECRET):
        _post_referral(
            db_session, {"crm_subscriber_id": str(crm_id), "referral_id": "r-1"}
        )
        _post_referral(
            db_session, {"crm_subscriber_id": str(crm_id), "referral_id": "r-2"}
        )

    # Different bodies -> different signatures -> different delivery ids.
    assert db_session.query(ReferralMirror).count() == 2
    assert db_session.query(CrmWebhookDelivery).count() == 2


# ── chat push dedup ───────────────────────────────────────────────────────


def _post_chat(db_session, body: dict):
    raw = json.dumps(body).encode()
    req = _FakeRequest(raw, _headers(raw, "message.outbound"))
    return _run(receive_crm_chat_event(req, db_session))


def test_chat_redelivery_does_not_double_push(db_session):
    body = {
        "subscriber_id": str(uuid.uuid4()),
        "preview": "hi",
        "conversation_id": "c1",
    }
    with _with_secret(SECRET), patch("app.services.push.send_push") as send_push:
        first = _post_chat(db_session, body)
        second = _post_chat(db_session, body)

    assert first["status"] == "ok"
    assert second["status"] == "ignored" and second["reason"] == "duplicate"
    assert send_push.call_count == 1  # the replay did not wake the device again


# ── delivery id derivation ────────────────────────────────────────────────


def test_delivery_uuid_prefers_header_over_signature():
    did = str(uuid.uuid4())
    req = _FakeRequest(
        b"{}", {"X-Webhook-Delivery-Id": did, "X-Webhook-Signature-256": "sha256=abc"}
    )
    assert _delivery_uuid(req) == uuid.UUID(did)


def test_delivery_uuid_falls_back_to_signature_deterministically():
    req_a = _FakeRequest(b"{}", {"X-Webhook-Signature-256": "sha256=abc"})
    req_b = _FakeRequest(b"{}", {"X-Webhook-Signature-256": "sha256=abc"})
    req_c = _FakeRequest(b"{}", {"X-Webhook-Signature-256": "sha256=xyz"})
    assert _delivery_uuid(req_a) == _delivery_uuid(req_b)
    assert _delivery_uuid(req_a) != _delivery_uuid(req_c)


# ── work-order mirror dedup (P-3) ─────────────────────────────────────────


def _post_work_order(db_session, body: dict, event="work_order.created"):
    raw = json.dumps(body).encode()
    return _run(
        receive_crm_work_order_event(
            _FakeRequest(raw, _headers(raw, event)), db_session
        )
    )


def test_work_order_redelivery_is_deduped(db_session):
    crm_id = uuid.uuid4()
    _linked_subscriber(db_session, crm_id)
    body = {
        "crm_subscriber_id": str(crm_id),
        "id": "wo-1",
        "status": "scheduled",
        "title": "Install",
    }

    with _with_secret(SECRET):
        first = _post_work_order(db_session, body)
        second = _post_work_order(db_session, body)

    assert first["status"] == "ok"
    assert second["status"] == "ignored" and second["reason"] == "duplicate"
    # The mirror upsert ran exactly once; the redelivery never re-applied.
    assert (
        db_session.query(WorkOrderMirror).filter_by(crm_work_order_id="wo-1").count()
        == 1
    )
    assert db_session.query(CrmWebhookDelivery).count() == 1


def test_distinct_work_order_events_both_process(db_session):
    crm_id = uuid.uuid4()
    _linked_subscriber(db_session, crm_id)

    with _with_secret(SECRET):
        _post_work_order(
            db_session,
            {"crm_subscriber_id": str(crm_id), "id": "wo-1", "status": "scheduled"},
        )
        # Different body -> different signature -> different delivery id: a real
        # later transition still applies (dedup only catches byte-identical
        # redeliveries, not distinct events).
        _post_work_order(
            db_session,
            {"crm_subscriber_id": str(crm_id), "id": "wo-1", "status": "in_progress"},
            event="work_order.updated",
        )

    row = db_session.query(WorkOrderMirror).filter_by(crm_work_order_id="wo-1").one()
    assert row.status == "in_progress"
    assert db_session.query(CrmWebhookDelivery).count() == 2
