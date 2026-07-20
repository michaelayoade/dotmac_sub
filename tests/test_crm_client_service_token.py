"""Auth-unification phase 2b: the CRM client prefers a static service ApiKey.

When ``service_token`` (settings.crm_service_token / CRM_SERVICE_TOKEN) is set,
requests authenticate with ``X-API-Key`` and the staff username/password
session->JWT login is never performed. Unset, the staff login still runs, so the
cut-over is a pure config flip.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.crm_client import (
    _REACHABILITY_CIRCUIT,
    CRMClient,
    CRMClientError,
)


@pytest.fixture(autouse=True)
def _reset_circuit():
    _REACHABILITY_CIRCUIT.reset()
    yield
    _REACHABILITY_CIRCUIT.reset()


def _capturing_client(captured: dict):
    """Pooled-client stand-in that records the headers of the outbound request."""

    def factory(*_a, **_k):
        inner = MagicMock()

        def _request(*_ra, **rk):
            captured["headers"] = rk.get("headers", {})
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "{}"
            resp.json.return_value = {}
            return resp

        inner.request.side_effect = _request
        return inner

    return factory


def test_service_token_sends_api_key():
    client = CRMClient("https://crm.example", service_token="svc-key-123")
    captured: dict = {}
    with patch("app.services.crm_client._pooled_client", _capturing_client(captured)):
        client._request("GET", "/api/v1/subscribers")

    assert captured["headers"]["X-API-Key"] == "svc-key-123"
    assert "Authorization" not in captured["headers"]


def test_no_service_token_fails_loudly():
    """The staff username/password->JWT fallback is retired (auth S1)."""
    client = CRMClient("https://crm.example", "user", "pass")
    with pytest.raises(CRMClientError, match="staff-credential login"):
        client._auth_headers()


def test_per_request_headers_still_override_but_api_key_stays():
    """Portal-scoped reads pass their own Authorization; the key rides alongside."""
    client = CRMClient("https://crm.example", service_token="svc-key-123")
    captured: dict = {}
    with patch("app.services.crm_client._pooled_client", _capturing_client(captured)):
        client._request(
            "GET", "/api/v1/portal/x", headers={"Authorization": "Bearer portal-tok"}
        )

    assert captured["headers"]["X-API-Key"] == "svc-key-123"
    assert captured["headers"]["Authorization"] == "Bearer portal-tok"


def test_auth_headers_requires_base_url():
    client = CRMClient("", "user", "pass", service_token="svc-key-123")
    with pytest.raises(CRMClientError):
        client._auth_headers()


def test_factory_resolves_secret_references(monkeypatch):
    """Integration credentials may be OpenBao/env refs, not plaintext (S5)."""
    from app.config import settings
    from app.services import crm_client as mod

    monkeypatch.setenv("CRM_TOKEN_FOR_TEST", "resolved-svc-key")
    original = settings.crm_service_token
    object.__setattr__(settings, "crm_service_token", "env://CRM_TOKEN_FOR_TEST")
    try:
        client = mod.get_crm_client(db=object())
        assert client.service_token == "resolved-svc-key"
    finally:
        object.__setattr__(settings, "crm_service_token", original)
