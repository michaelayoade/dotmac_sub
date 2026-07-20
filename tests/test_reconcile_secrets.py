"""Tests for the reconciler's secret resolver."""

from __future__ import annotations

import pytest

from app.services.network.reconcile.secrets import (
    SecretResolutionError,
    credential_secret_resolver,
    default_secret_resolver_from_env,
    openbao_secret_resolver,
)

# ── openbao_secret_resolver ────────────────────────────────────────────────


def test_openbao_resolver_empty_ref_returns_empty():
    """Empty / None ref returns empty string — writing-back an empty value
    is meaningful and shouldn't crash the resolver."""
    assert openbao_secret_resolver("") == ""


def test_openbao_resolver_plaintext_passes_through():
    """A plaintext value that doesn't look like a secret URI is returned
    unchanged. This preserves the migration path: callers can still pass
    plaintext while we move them onto bao:// refs gradually."""
    assert openbao_secret_resolver("kursimining@98765") == "kursimining@98765"
    assert openbao_secret_resolver("admin") == "admin"


def test_openbao_resolver_routes_bao_uri_through_resolve_secret(monkeypatch):
    captured: list[str] = []

    def _fake_resolve(value):
        captured.append(value)
        return "actual-secret-value"

    monkeypatch.setattr(
        "app.services.network.reconcile.secrets.resolve_secret",
        _fake_resolve,
    )
    result = openbao_secret_resolver("bao://secret/wifi#psk")
    assert result == "actual-secret-value"
    assert captured == ["bao://secret/wifi#psk"]


def test_openbao_resolver_translates_exception_to_typed_error(monkeypatch):
    """Any exception from resolve_secret (OpenBao 5xx, network timeout,
    missing KV field) surfaces as SecretResolutionError so the applier
    can map it cleanly."""

    def _exploding_resolve(value):
        raise RuntimeError("OpenBao 503 unreachable")

    monkeypatch.setattr(
        "app.services.network.reconcile.secrets.resolve_secret",
        _exploding_resolve,
    )
    with pytest.raises(SecretResolutionError) as exc_info:
        openbao_secret_resolver("bao://secret/wifi#psk")
    assert "bao://secret/wifi#psk" in str(exc_info.value)
    assert "OpenBao 503 unreachable" in str(exc_info.value)


def test_openbao_resolver_treats_none_return_as_error(monkeypatch):
    """resolve_secret returning None means OpenBao succeeded but the
    field was empty — distinguishable failure mode from missing path."""
    monkeypatch.setattr(
        "app.services.network.reconcile.secrets.resolve_secret",
        lambda _v: None,
    )
    with pytest.raises(SecretResolutionError) as exc_info:
        openbao_secret_resolver("bao://secret/wifi#psk")
    assert "None" in str(exc_info.value)


def test_openbao_resolver_handles_env_uri(monkeypatch):
    """env://VAR is also handled by resolve_secret."""
    monkeypatch.setattr(
        "app.services.network.reconcile.secrets.resolve_secret",
        lambda v: "ENV_VALUE" if v == "env://MY_VAR" else None,
    )
    result = openbao_secret_resolver("env://MY_VAR")
    assert result == "ENV_VALUE"


# ── default_secret_resolver_from_env ───────────────────────────────────────


def test_default_resolver_always_handles_encryption_at_rest():
    assert default_secret_resolver_from_env() is credential_secret_resolver


def test_credential_resolver_decrypts_local_wrappers(monkeypatch):
    monkeypatch.setattr(
        "app.services.network.reconcile.secrets.decrypt_credential",
        lambda value: "decrypted" if value == "enc:ciphertext" else value,
    )
    assert credential_secret_resolver("enc:ciphertext") == "decrypted"
