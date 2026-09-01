"""DB-free CRM HTTP substrate authentication behavior."""

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


def test_global_settings_factory_is_removed():
    from app.services import crm_client as mod

    assert not hasattr(mod, "get_crm_client")


def test_widget_session_minting_is_gone():
    """ADR 0006's live-chat transport was retired 2026-08-30 with the CRM.

    Asserted here rather than merely deleted: `create_widget_session` was the
    one CRM POST that existed purely to hand portal chat to another system, and
    its absence is what makes Sub's Team Inbox the sole live-chat authority.
    `tests/architecture/test_no_crm_writeback.py` also removed its exemption
    from the outbound-write allow-list.
    """

    assert not hasattr(CRMClient, "create_widget_session")
