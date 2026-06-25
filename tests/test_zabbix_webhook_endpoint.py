"""Endpoint behaviour for the Zabbix alert webhook.

Pins the auth-before-parse contract: unauthenticated callers must get 401
(not 422) so scanners/misconfigured senders neither generate validation
noise nor learn the payload schema. Authenticated-but-malformed bodies get
422 and are logged for reconciling the Zabbix action template.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import zabbix_webhook

_SECRET = "test-zabbix-secret"

_VALID_PAYLOAD = {
    "triggerId": "1001",
    "triggerName": "High CPU",
    "triggerStatus": "PROBLEM",
    "triggerSeverity": "High",
    "hostId": "20001",
    "hostName": "edge-router-1",
    "eventId": "evt-1",
}


@pytest.fixture
def zabbix_auth(monkeypatch):
    monkeypatch.setattr(zabbix_webhook, "get_zabbix_webhook_token", lambda: _SECRET)

    async def _inline_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(zabbix_webhook, "run_in_threadpool", _inline_threadpool)


_URL = "/api/v1/zabbix/webhook/alert"


def _request(content: bytes) -> Request:
    async def _receive():
        return {"type": "http.request", "body": content, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": _URL,
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        },
        _receive,
    )


def _call(db_session, content: bytes, *, token: str | None = None):
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(
            asyncio.run,
            zabbix_webhook.receive_zabbix_alert(
                request=_request(content),
                db=db_session,
                x_zabbix_token=token,
            ),
        ).result()


def test_unauthenticated_request_is_401_not_422(db_session, zabbix_auth):
    """Garbage body without a token must be rejected at auth, before parsing."""
    with pytest.raises(HTTPException) as exc:
        _call(db_session, b"not even json")
    assert exc.value.status_code == 401


def test_wrong_token_is_401(db_session, zabbix_auth):
    with pytest.raises(HTTPException) as exc:
        _call(db_session, json.dumps(_VALID_PAYLOAD).encode(), token="wrong")
    assert exc.value.status_code == 401


def test_authenticated_invalid_payload_is_422_and_logged(
    db_session, zabbix_auth, caplog
):
    """A real (authenticated) but malformed payload returns 422 and logs the
    raw body so the Zabbix action template can be reconciled."""
    with caplog.at_level("WARNING"):
        with pytest.raises(HTTPException) as exc:
            _call(
                db_session,
                json.dumps({"unexpected": "shape"}).encode(),
                token=_SECRET,
            )
    assert exc.value.status_code == 422
    assert any(
        r.message == "zabbix_webhook_invalid_payload"
        or getattr(r, "event", None) == "zabbix_webhook_invalid_payload"
        for r in caplog.records
    )


def test_authenticated_valid_payload_creates_alert(db_session, zabbix_auth):
    body = _call(db_session, json.dumps(_VALID_PAYLOAD).encode(), token=_SECRET)
    assert body.status == "ok"
    assert body.alert_id
