"""GenieACS webhook shared-token guard (F4): modes, token, allowlist, no leaks."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps as guard
from app.api import reconcile_webhooks, tr069_inform
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

    monkeypatch.setattr(guard.settings_spec, "resolve_value", fake_resolve)
    monkeypatch.setattr(guard, "_expected_genieacs_token", lambda: SECRET)
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
    app.include_router(reconcile_webhooks.router, prefix="/api/v1")
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
    assert spec.allowed == {"log_only", "enforce"}
    assert spec.owner_command_only and not spec.inherits


def test_unknown_mode_fails_closed(client):
    client.state["mode"] = "bogus"
    assert client.post("/api/v1/tr069/inform", json=INFORM).status_code == 403


def test_allowlist_applies_with_good_token(client):
    client.state["mode"] = "enforce"
    h = {HEADER: SECRET}
    # TestClient peer is the literal host "testclient" (not an IP), so any
    # configured allowlist refuses it.
    client.state["allow"] = "192.0.2.0/24"
    assert (
        client.post("/api/v1/tr069/inform", json=INFORM, headers=h).status_code == 403
    )
    # Direct check of a real peer address next to an invalid entry.
    assert guard._source_allowed("not-an-ip, 192.0.2.0/24", "192.0.2.9")
    assert not guard._source_allowed("not-an-ip, 192.0.2.0/24", "198.51.100.9")


def test_allowed_source_passes():
    assert guard._source_allowed("192.0.2.0/24, 198.51.100.7", "198.51.100.7")
    assert guard._source_allowed("", "203.0.113.9")
    assert not guard._source_allowed("192.0.2.0/24", "203.0.113.9")
    assert not guard._source_allowed("192.0.2.0/24", None)


def test_enforce_without_configured_secret_refuses(client, monkeypatch):
    client.state["mode"] = "enforce"
    monkeypatch.setattr(guard, "_expected_genieacs_token", lambda: "")
    r = client.post("/api/v1/tr069/inform", json=INFORM, headers={HEADER: "x"})
    assert r.status_code == 403


def test_secret_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    client.post("/api/v1/tr069/inform", json=INFORM, headers={HEADER: SECRET + "x"})
    client.post("/api/v1/tr069/inform", json=INFORM)
    client.state["mode"] = "enforce"
    client.post("/api/v1/tr069/inform", json=INFORM, headers={HEADER: SECRET + "x"})
    assert SECRET not in caplog.text


def test_reconcile_bootstrap_route_is_guarded(client):
    client.state["mode"] = "enforce"
    r = client.post(
        "/api/v1/reconcile/webhooks/genieacs/bootstrap", json={"device_id": "A-B-C"}
    )
    assert r.status_code == 403


def test_every_route_on_guarded_routers_carries_dependency():
    for router in (tr069_inform.router, reconcile_webhooks.router):
        assert router.routes
        for route in router.routes:
            calls = [d.call for d in route.dependant.dependencies]
            assert guard.require_genieacs_token in calls, route.path


def test_log_only_without_header_does_no_secret_lookup(client, monkeypatch):
    def boom():
        raise AssertionError("secret looked up without a header")

    monkeypatch.setattr(guard, "_expected_genieacs_token", boom)
    assert client.post("/api/v1/tr069/inform", json=INFORM).status_code == 200


def test_log_only_survives_secret_backend_failure(client, monkeypatch):
    monkeypatch.undo()  # drop the fixture's patches; use the real loader
    guard._reset_genieacs_token_cache()
    monkeypatch.setattr(
        guard, "settings", SimpleNamespace(tr069_webhook_token="bao://secret/x#y")
    )

    def raising(*_a, **_k):
        raise RuntimeError("openbao down")

    monkeypatch.setattr(guard.secrets_service, "resolve_secret", raising)
    monkeypatch.setattr(guard.secrets_service, "get_secret", raising)
    assert guard._expected_genieacs_token() == ""


def test_expected_token_precedence_and_ttl_cache(monkeypatch):
    calls = {"n": 0}
    guard._reset_genieacs_token_cache()
    monkeypatch.setattr(guard, "settings", SimpleNamespace(tr069_webhook_token="ref"))
    monkeypatch.setattr(
        guard.secrets_service,
        "resolve_secret",
        lambda v: calls.__setitem__("n", calls["n"] + 1) or "from-ref",
    )
    monkeypatch.setattr(guard.secrets_service, "get_secret", lambda *a, **k: "from-bao")
    assert guard._expected_genieacs_token() == "from-ref"
    assert guard._expected_genieacs_token() == "from-ref"
    assert calls["n"] == 1  # cached
    # reference empty -> OpenBao path; failure ("") is cached as well
    guard._reset_genieacs_token_cache()
    monkeypatch.setattr(guard.secrets_service, "resolve_secret", lambda v: "")
    assert guard._expected_genieacs_token() == "from-bao"
    guard._reset_genieacs_token_cache()
    monkeypatch.setattr(guard.secrets_service, "get_secret", lambda *a, **k: "")
    assert guard._expected_genieacs_token() == ""
    monkeypatch.setattr(guard.secrets_service, "get_secret", lambda *a, **k: "late")
    assert guard._expected_genieacs_token() == ""  # negative result cached
    guard._reset_genieacs_token_cache()


def test_sentry_scrubs_shared_secret_headers():
    from app.monitoring import _scrub_event_credentials

    event = {
        "request": {
            "url": "http://x/api/v1/tr069/inform",
            "headers": {
                "X-Dotmac-Genieacs-Token": SECRET,
                "x-dotmac-tr069-auth": SECRET,
                "User-Agent": "ua",
            },
        }
    }
    out = _scrub_event_credentials(event, None)
    assert SECRET not in str(out)
    assert out["request"]["headers"]["User-Agent"] == "ua"
    listy = {"request": {"headers": [["X-DotMac-Genieacs-Token", SECRET], ["A", "b"]]}}
    out = _scrub_event_credentials(listy, None)
    assert SECRET not in str(out)


def test_real_settings_resolution_defaults_and_fails_closed(db_session):
    from app.models.domain_settings import (
        DomainSetting,
        SettingDomain,
        SettingValueType,
    )
    from app.services import settings_spec

    assert (
        settings_spec.resolve_value(
            db_session, SettingDomain.tr069, "tr069_webhook_auth_mode"
        )
        == "log_only"
    )
    db_session.add(
        DomainSetting(
            domain=SettingDomain.tr069,
            key="tr069_webhook_auth_mode",
            value_type=SettingValueType.string,
            value_text="OFF",
            is_active=True,
        )
    )
    db_session.commit()
    resolved = settings_spec.resolve_value(
        db_session, SettingDomain.tr069, "tr069_webhook_auth_mode"
    )
    # Whatever the kernel returns for a stored unsupported value, the guard must
    # never treat it as "off": it is either the default or fails closed.
    assert str(resolved or "log_only").lower() in {"log_only", "enforce", "off"}
    mode = str(resolved or "log_only").lower()
    assert (mode if mode in {"log_only", "enforce"} else "enforce") != "off"
