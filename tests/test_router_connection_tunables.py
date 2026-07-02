"""Router REST + CoA tunables are settings-driven, with safe fallbacks.

Guards the audit fix: connection timeouts/retries/backoff and the CoA negative
cache TTL must come from SettingDomain.network (operators tune WAN/high-latency
plant without a code change) and must fall back to the module defaults if
settings/DB are unavailable — the connection/enforcement layers must never break
on a settings hiccup.
"""

from __future__ import annotations

from datetime import timedelta

from app.services import enforcement
from app.services.router_management import connection


class _DummyCtx:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def _patch(monkeypatch, resolver):
    # No real DB needed: stub the session and the settings resolver.
    monkeypatch.setattr("app.db.SessionLocal", lambda: _DummyCtx())
    monkeypatch.setattr("app.services.settings_spec.resolve_value", resolver)


def test_rest_tunables_from_settings(monkeypatch):
    vals = {
        "router_rest_connect_timeout_seconds": 20,
        "router_rest_read_timeout_seconds": 90,
        "router_rest_max_retries": 5,
        "router_rest_retry_backoff_base": "1.5",
    }
    _patch(monkeypatch, lambda db, domain, key, **k: vals.get(key))
    assert connection._rest_tunables() == (20.0, 90.0, 5, 1.5)


def test_rest_tunables_fallback_to_defaults(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("settings/DB down")

    _patch(monkeypatch, boom)
    assert connection._rest_tunables() == (
        connection.CONNECT_TIMEOUT,
        connection.READ_TIMEOUT,
        connection.MAX_RETRIES,
        connection.RETRY_BACKOFF_BASE,
    )


def test_rest_tunables_partial_settings_keep_defaults(monkeypatch):
    # Only read timeout set; the rest fall back to module defaults.
    _patch(
        monkeypatch,
        lambda db, domain, key, **k: (
            90 if key == "router_rest_read_timeout_seconds" else None
        ),
    )
    ct, rt, mr, bb = connection._rest_tunables()
    assert rt == 90.0
    assert (ct, mr, bb) == (
        connection.CONNECT_TIMEOUT,
        connection.MAX_RETRIES,
        connection.RETRY_BACKOFF_BASE,
    )


def test_coa_neg_ttl_from_settings(monkeypatch):
    _patch(monkeypatch, lambda db, domain, key, **k: 40)
    assert enforcement._coa_neg_ttl() == timedelta(minutes=40)


def test_coa_neg_ttl_fallback(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    _patch(monkeypatch, boom)
    assert enforcement._coa_neg_ttl() == enforcement._COA_NEG_TTL
