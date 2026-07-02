"""Control-plane audit phase 2/3: secret length + api-port are configurable."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.nas import _mikrotik
from app.web.admin import nas as nas_admin


class _DummyCtx:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def _patch_settings(monkeypatch, resolver):
    monkeypatch.setattr("app.db.SessionLocal", lambda: _DummyCtx())
    monkeypatch.setattr("app.services.settings_spec.resolve_value", resolver)


def test_generated_secret_length_from_settings(monkeypatch):
    _patch_settings(monkeypatch, lambda db, domain, key, **k: 40)
    assert len(nas_admin._generate_radius_shared_secret()) == 40


def test_generated_secret_length_fallback_is_strong(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("settings down")

    _patch_settings(monkeypatch, boom)
    # Never the old weak 6 — falls back to the strong default (32).
    assert len(nas_admin._generate_radius_shared_secret()) == 32


def test_generated_secret_length_floor(monkeypatch):
    _patch_settings(monkeypatch, lambda db, domain, key, **k: 4)  # below floor
    assert len(nas_admin._generate_radius_shared_secret()) == 16


def test_mikrotik_api_port_prefers_column():
    dev = SimpleNamespace(mikrotik_api_port=8729, tags=["mikrotik_api_port:9999"])
    assert _mikrotik._mikrotik_api_port(dev) == 8729


def test_mikrotik_api_port_falls_back_to_tag_then_default():
    tag_only = SimpleNamespace(mikrotik_api_port=None, tags=["mikrotik_api_port:8730"])
    assert _mikrotik._mikrotik_api_port(tag_only) == 8730
    neither = SimpleNamespace(mikrotik_api_port=None, tags=[])
    assert _mikrotik._mikrotik_api_port(neither) == 8728
