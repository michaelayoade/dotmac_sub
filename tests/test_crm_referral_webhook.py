"""Retired CRM referral webhook tombstone: authenticated 200/no-op only."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from contextlib import contextmanager

from fastapi import HTTPException

from app.api.crm_webhooks import receive_crm_referral_event
from app.config import settings
from app.models.referral import ReferralMirror

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


def _post(db_session, body: object, event: str, signature=...):
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


def test_valid_legacy_delivery_is_authenticated_and_ignored(db_session):
    with _with_secret(SECRET):
        code, response = _post(
            db_session,
            {"referral_id": "legacy-r-1"},
            event="referral.rewarded",
        )

    assert code == 200
    assert response == {
        "status": "ignored",
        "reason": "crm_referral_path_retired",
        "event": "referral.rewarded",
    }
    assert db_session.query(ReferralMirror).count() == 0


def test_unknown_delivery_is_also_a_noop(db_session):
    with _with_secret(SECRET):
        code, response = _post(db_session, ["not", "an", "object"], "anything")

    assert code == 200
    assert response["reason"] == "crm_referral_path_retired"
    assert db_session.query(ReferralMirror).count() == 0


def test_bad_signature_is_still_rejected(db_session):
    with _with_secret(SECRET):
        code, _ = _post(
            db_session,
            {"referral_id": "legacy-r-1"},
            event="referral.captured",
            signature="sha256=deadbeef",
        )
    assert code == 401
