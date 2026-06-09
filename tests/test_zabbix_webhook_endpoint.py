"""Endpoint behaviour for the Zabbix alert webhook.

Pins the auth-before-parse contract: unauthenticated callers must get 401
(not 422) so scanners/misconfigured senders neither generate validation
noise nor learn the payload schema. Authenticated-but-malformed bodies get
422 and are logged for reconciling the Zabbix action template.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import zabbix_webhook
from app.api.zabbix_webhook import router as zabbix_router
from app.db import get_db

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
def client(db_session, monkeypatch):
    monkeypatch.setattr(zabbix_webhook, "get_zabbix_webhook_token", lambda: _SECRET)

    app = FastAPI()
    app.include_router(zabbix_router, prefix="/api/v1")

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app, raise_server_exceptions=False)


_URL = "/api/v1/zabbix/webhook/alert"


def test_unauthenticated_request_is_401_not_422(client):
    """Garbage body without a token must be rejected at auth, before parsing."""
    resp = client.post(
        _URL,
        content=b"not even json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_wrong_token_is_401(client):
    resp = client.post(
        _URL,
        content=json.dumps(_VALID_PAYLOAD),
        headers={"X-Zabbix-Token": "wrong", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_authenticated_invalid_payload_is_422_and_logged(client, caplog):
    """A real (authenticated) but malformed payload returns 422 and logs the
    raw body so the Zabbix action template can be reconciled."""
    with caplog.at_level("WARNING"):
        resp = client.post(
            _URL,
            content=json.dumps({"unexpected": "shape"}),
            headers={"X-Zabbix-Token": _SECRET, "Content-Type": "application/json"},
        )
    assert resp.status_code == 422
    assert any(
        r.message == "zabbix_webhook_invalid_payload"
        or getattr(r, "event", None) == "zabbix_webhook_invalid_payload"
        for r in caplog.records
    )


def test_authenticated_valid_payload_creates_alert(client):
    resp = client.post(
        _URL,
        content=json.dumps(_VALID_PAYLOAD),
        headers={"X-Zabbix-Token": _SECRET, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["alert_id"]
