"""Inbound CRM referral webhook endpoint (RFC #73): HMAC gate + delegation.

The handler is a thin wrapper — the mirror/credit logic is unit-tested in
test_referrals_mirror.py. Here we cover the signature gate, event filtering, and
that a valid event reaches the service (a mirror row appears).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import uuid
from contextlib import contextmanager

from fastapi import HTTPException

from app.api.crm_webhooks import receive_crm_referral_event
from app.config import settings
from app.models.referral import ReferralMirror
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


def _post(db_session, body: dict, event: str = "referral.captured", signature=...):
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


def test_valid_captured_event_reaches_service(db_session):
    crm_id = uuid.uuid4()
    _linked_subscriber(db_session, crm_id)
    body = {
        "crm_subscriber_id": str(crm_id),
        "referral_id": "r-1",
        "referred_name": "Ada",
    }
    with _with_secret(SECRET):
        code, resp = _post(db_session, body)
    assert code == 200, resp
    assert resp["status"] == "ok"
    assert (
        db_session.query(ReferralMirror).filter_by(crm_referral_id="r-1").count() == 1
    )


def test_event_envelope_form_is_accepted(db_session):
    crm_id = uuid.uuid4()
    _linked_subscriber(db_session, crm_id)
    body = {"payload": {"crm_subscriber_id": str(crm_id), "referral_id": "r-2"}}
    with _with_secret(SECRET):
        code, resp = _post(db_session, body, event="referral.qualified")
    assert code == 200
    row = db_session.query(ReferralMirror).filter_by(crm_referral_id="r-2").one()
    assert row.status == "qualified"


def test_bad_signature_rejected(db_session):
    body = {"crm_subscriber_id": str(uuid.uuid4()), "referral_id": "x"}
    with _with_secret(SECRET):
        code, _ = _post(db_session, body, signature="sha256=deadbeef")
    assert code == 401


def test_unknown_event_ignored(db_session):
    body = {"crm_subscriber_id": str(uuid.uuid4()), "referral_id": "x"}
    with _with_secret(SECRET):
        code, resp = _post(db_session, body, event="referral.expired")
    assert code == 200
    assert resp["status"] == "ignored"
