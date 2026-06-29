"""Inbound CRM ``referral.rewarded`` webhook: HMAC auth + account credit (RFC #73)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import uuid
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.api.crm_webhooks import receive_crm_referral_event
from app.config import settings
from app.models.subscriber import Subscriber

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
    # Drive the async route on a fresh loop in a dedicated thread (matches
    # test_crm_webhooks): immune to a leaked running loop; the test SQLite engine
    # is check_same_thread=False + StaticPool so the session is safe cross-thread.
    box: dict[str, object] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["result"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on caller thread
            box["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=_runner)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]


def _post(db_session, body: dict, event: str = "referral.rewarded", signature=...):
    raw = json.dumps(body).encode()
    headers = {"X-Webhook-Event": event, "Content-Type": "application/json"}
    sig = _sign(raw) if signature is ... else signature
    if sig is not None:
        headers["X-Webhook-Signature-256"] = sig
    try:
        payload = _run(
            receive_crm_referral_event(_FakeRequest(raw, headers), db_session)
        )
    except HTTPException as exc:
        return exc.status_code, {"detail": exc.detail}
    return 200, payload


def _linked_subscriber(db_session, crm_id: uuid.UUID) -> Subscriber:
    sub = Subscriber(
        first_name="Ref",
        last_name="Errer",
        email=f"r-{uuid.uuid4().hex[:8]}@example.com",
        crm_subscriber_id=crm_id,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _credit_patch():
    credit = MagicMock()
    credit.id = uuid.uuid4()
    return (
        patch(
            "app.api.crm_webhooks.crm_api.create_account_credit", return_value=credit
        ),
        patch("app.services.push.send_push"),
        credit,
    )


def test_reward_credits_mapped_subscriber(db_session):
    crm_id = uuid.uuid4()
    sub = _linked_subscriber(db_session, crm_id)
    body = {
        "crm_subscriber_id": str(crm_id),
        "referral_id": "ref-123",
        "amount": "5000",
        "currency": "NGN",
    }
    credit_p, push_p, credit = _credit_patch()
    with _with_secret(SECRET), credit_p as create_credit, push_p:
        code, resp = _post(db_session, body)

    assert code == 200, resp
    assert resp["status"] == "ok"
    assert resp["credit_id"] == str(credit.id)
    kwargs = create_credit.call_args.kwargs
    assert kwargs["subscriber_id"] == str(sub.id)
    assert kwargs["amount"] == Decimal("5000")
    assert kwargs["external_ref"] == "referral:ref-123"
    assert kwargs["currency"] == "NGN"


def test_reward_accepts_event_envelope(db_session):
    crm_id = uuid.uuid4()
    _linked_subscriber(db_session, crm_id)
    body = {
        "payload": {
            "crm_subscriber_id": str(crm_id),
            "referral_id": "ref-9",
            "amount": 2500,
        }
    }
    credit_p, push_p, _ = _credit_patch()
    with _with_secret(SECRET), credit_p as create_credit, push_p:
        code, resp = _post(db_session, body)
    assert code == 200
    assert resp["status"] == "ok"
    assert create_credit.call_args.kwargs["amount"] == Decimal("2500")


def test_bad_signature_rejected(db_session):
    body = {"crm_subscriber_id": str(uuid.uuid4()), "referral_id": "x", "amount": "1"}
    with _with_secret(SECRET):
        code, _ = _post(db_session, body, signature="sha256=deadbeef")
    assert code == 401


def test_unknown_event_ignored(db_session):
    body = {"crm_subscriber_id": str(uuid.uuid4()), "referral_id": "x", "amount": "1"}
    with _with_secret(SECRET):
        code, resp = _post(db_session, body, event="referral.captured")
    assert code == 200
    assert resp["status"] == "ignored"


def test_unmapped_subscriber_ignored_without_credit(db_session):
    body = {
        "crm_subscriber_id": str(uuid.uuid4()),
        "referral_id": "ref-x",
        "amount": "5000",
    }
    credit_p, push_p, _ = _credit_patch()
    with _with_secret(SECRET), credit_p as create_credit, push_p:
        code, resp = _post(db_session, body)
    assert code == 200
    assert resp["reason"] == "unmapped_subscriber"
    create_credit.assert_not_called()


def test_incomplete_payload_ignored(db_session):
    crm_id = uuid.uuid4()
    _linked_subscriber(db_session, crm_id)
    body = {"crm_subscriber_id": str(crm_id), "referral_id": "ref-1"}  # no amount
    credit_p, push_p, _ = _credit_patch()
    with _with_secret(SECRET), credit_p as create_credit, push_p:
        code, resp = _post(db_session, body)
    assert code == 200
    assert resp["reason"] == "incomplete_payload"
    create_credit.assert_not_called()


def test_non_positive_amount_ignored(db_session):
    crm_id = uuid.uuid4()
    _linked_subscriber(db_session, crm_id)
    body = {"crm_subscriber_id": str(crm_id), "referral_id": "ref-1", "amount": "0"}
    credit_p, push_p, _ = _credit_patch()
    with _with_secret(SECRET), credit_p as create_credit, push_p:
        code, resp = _post(db_session, body)
    assert code == 200
    assert resp["reason"] == "non_positive_amount"
    create_credit.assert_not_called()
