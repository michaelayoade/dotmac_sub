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
    """httpx.Client factory that records the headers of the outbound request."""

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
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=inner)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    return factory


def test_service_token_sends_api_key_and_skips_staff_login():
    client = CRMClient(
        "https://crm.example", "user", "pass", service_token="svc-key-123"
    )
    captured: dict = {}
    with patch("app.services.crm_client.httpx.Client", _capturing_client(captured)):
        # If this touched _ensure_token it would attempt a real login and fail;
        # boobytrap it to prove the staff path is never taken.
        with patch.object(
            client,
            "_ensure_token",
            side_effect=AssertionError("staff login must not run with a service token"),
        ):
            client._request("GET", "/api/v1/subscribers")

    assert captured["headers"]["X-API-Key"] == "svc-key-123"
    assert "Authorization" not in captured["headers"]


def test_no_service_token_uses_staff_bearer():
    client = CRMClient("https://crm.example", "user", "pass")
    captured: dict = {}
    with patch("app.services.crm_client.httpx.Client", _capturing_client(captured)):
        with patch.object(client, "_ensure_token", return_value="jwt-tok"):
            client._request("GET", "/api/v1/subscribers")

    assert captured["headers"]["Authorization"] == "Bearer jwt-tok"
    assert "X-API-Key" not in captured["headers"]


def test_per_request_headers_still_override_but_api_key_stays():
    """Portal-scoped reads pass their own Authorization; the key rides alongside."""
    client = CRMClient(
        "https://crm.example", "user", "pass", service_token="svc-key-123"
    )
    captured: dict = {}
    with patch("app.services.crm_client.httpx.Client", _capturing_client(captured)):
        client._request(
            "GET", "/api/v1/portal/x", headers={"Authorization": "Bearer portal-tok"}
        )

    assert captured["headers"]["X-API-Key"] == "svc-key-123"
    assert captured["headers"]["Authorization"] == "Bearer portal-tok"


def test_auth_headers_requires_base_url():
    client = CRMClient("", "user", "pass", service_token="svc-key-123")
    with pytest.raises(CRMClientError):
        client._auth_headers()
