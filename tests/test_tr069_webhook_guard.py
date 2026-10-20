"""GenieACS webhook shared-token guard (F4): modes, token, allowlist, no leaks."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import tr069_inform
from app.db import get_db

SECRET = "s3cr3t-token-value-for-tests"
HEADER = "X-Dotmac-Genieacs-Token"
INFORM = {"serial_number": "GUARD-001", "event": "periodic"}


@pytest.fixture()
def client(monkeypatch):
    state = {"mode": "log_only", "allow": None}

    def fake_resolve(db, domain, key):
        return {
            "tr069_webhook_auth_mode": state["mode"],
            "tr069_webhook_allowed_sources": state["allow"],
        }[key]

    monkeypatch.setattr(tr069_inform.settings_spec, "resolve_value", fake_resolve)
    monkeypatch.setattr(tr069_inform, "_expected_token", lambda: SECRET)
    calls: list[dict] = []
    monkeypatch.setattr(
        tr069_inform.genieacs_service,
        "receive_inform",
        lambda db, **kw: calls.append(kw) or {"ok": True},
    )
    monkeypatch.setattr(
        tr069_inform, "find_unique_active_ont_by_serial", lambda db, serial: None
    )
    app = FastAPI()
    app.include_router(tr069_inform.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: None
    c = TestClient(app)
    c.state = state  # type: ignore[attr-defined]
    c.calls = calls  # type: ignore[attr-defined]
    return c


def test_enforce_refuses_missing_and_bad_token(client):
    client.state["mode"] = "enforce"
    assert client.post("/api/v1/tr069/inform", json=INFORM).status_code == 403
    r = client.post("/api/v1/tr069/inform", json=INFORM, headers={HEADER: "nope"})
    assert r.status_code == 403
    assert client.get("/api/v1/tr069/device-config/X1").status_code == 403
    assert client.calls == []


def test_enforce_accepts_good_token(client):
    client.state["mode"] = "enforce"
    r = client.post("/api/v1/tr069/inform", json=INFORM, headers={HEADER: SECRET})
    assert r.status_code == 200
    # device-config passes the guard (then 404s: no such device)
    r = client.get("/api/v1/tr069/device-config/X1", headers={HEADER: SECRET})
    assert r.status_code == 404


def test_log_only_allows_but_logs(client, caplog):
    caplog.set_level(logging.WARNING)
    r = client.post("/api/v1/tr069/inform", json=INFORM)
    assert r.status_code == 200
    r = client.post("/api/v1/tr069/inform", json=INFORM, headers={HEADER: "bad"})
    assert r.status_code == 200
    text = caplog.text
    assert "reason=missing_token" in text and "reason=bad_token" in text
    assert "mode=log_only" in text


def test_default_mode_is_log_only():
    from app.services import settings_spec

    spec = next(
        s for s in settings_spec.SETTINGS_SPECS if s.key == "tr069_webhook_auth_mode"
    )
    assert spec.default == "log_only"
    assert spec.allowed == {"off", "log_only", "enforce"}


def test_off_skips_check(client):
    client.state["mode"] = "off"
    assert client.post("/api/v1/tr069/inform", json=INFORM).status_code == 200


def test_unknown_mode_fails_closed(client):
    client.state["mode"] = "bogus"
    assert client.post("/api/v1/tr069/inform", json=INFORM).status_code == 403


def test_allowlist_applies_with_good_token(client):
    client.state["mode"] = "enforce"
    client.state["allow"] = "192.0.2.0/24"  # testclient source is not in range
    r = client.post("/api/v1/tr069/inform", json=INFORM, headers={HEADER: SECRET})
    assert r.status_code == 403
    client.state["allow"] = "testclient, 192.0.2.0/24"  # invalid entry ignored
    assert (
        client.post(
            "/api/v1/tr069/inform", json=INFORM, headers={HEADER: SECRET}
        ).status_code
        == 403
    )


def test_allowed_source_passes():
    assert tr069_inform._source_allowed("192.0.2.0/24, 198.51.100.7", "198.51.100.7")
    assert tr069_inform._source_allowed("", "203.0.113.9")
    assert not tr069_inform._source_allowed("192.0.2.0/24", "203.0.113.9")
    assert not tr069_inform._source_allowed("192.0.2.0/24", None)


def test_enforce_without_configured_secret_refuses(client, monkeypatch):
    client.state["mode"] = "enforce"
    monkeypatch.setattr(tr069_inform, "_expected_token", lambda: "")
    r = client.post("/api/v1/tr069/inform", json=INFORM, headers={HEADER: "x"})
    assert r.status_code == 403


def test_secret_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    client.post("/api/v1/tr069/inform", json=INFORM, headers={HEADER: SECRET + "x"})
    client.post("/api/v1/tr069/inform", json=INFORM)
    client.state["mode"] = "enforce"
    client.post("/api/v1/tr069/inform", json=INFORM, headers={HEADER: SECRET + "x"})
    assert SECRET not in caplog.text
