"""Tests for secrets service."""

import pytest
from fastapi import HTTPException

from app.services import secrets
from tests.mocks import FakeHTTPXResponse


def test_is_openbao_ref_valid():
    """Test detecting valid OpenBao references."""
    assert secrets.is_openbao_ref("openbao://secret/data/myapp#password") is True
    assert secrets.is_openbao_ref("openbao://kv/data/config#api_key") is True
    assert secrets.is_openbao_ref("vault://secret/data/creds#token") is True


def test_is_openbao_ref_invalid():
    """Test detecting invalid OpenBao references."""
    assert secrets.is_openbao_ref("plain-text-secret") is False
    assert secrets.is_openbao_ref("https://example.com") is False
    assert secrets.is_openbao_ref("") is False
    assert secrets.is_openbao_ref(None) is False


def test_resolve_secret_passthrough_plain_value():
    """Test that plain values are passed through unchanged."""
    result = secrets.resolve_secret("my-plain-secret")
    assert result == "my-plain-secret"


def test_resolve_secret_none_value():
    """Test that None values return None."""
    result = secrets.resolve_secret(None)
    assert result is None


def test_resolve_openbao_ref_kv_v2(monkeypatch):
    """Test resolving OpenBao KV v2 reference."""
    # Mock environment
    monkeypatch.setenv("OPENBAO_ADDR", "https://vault.test.local:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")
    monkeypatch.setenv("OPENBAO_KV_VERSION", "2")

    # Mock httpx response
    mock_response = FakeHTTPXResponse(
        json_data={
            "data": {
                "data": {
                    "password": "secret-password-123",
                }
            }
        }
    )

    def mock_get(url, **kwargs):
        return mock_response

    # Patch httpx.get
    import httpx
    monkeypatch.setattr(httpx, "get", mock_get)

    result = secrets.resolve_openbao_ref("openbao://secret/data/myapp#password")
    assert result == "secret-password-123"


def test_resolve_openbao_ref_kv_v1(monkeypatch):
    """Test resolving OpenBao KV v1 reference."""
    monkeypatch.setenv("OPENBAO_ADDR", "https://vault.test.local:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")
    monkeypatch.setenv("OPENBAO_KV_VERSION", "1")

    mock_response = FakeHTTPXResponse(
        json_data={
            "data": {
                "api_key": "key-abc-123",
            }
        }
    )

    def mock_get(url, **kwargs):
        return mock_response

    import httpx
    monkeypatch.setattr(httpx, "get", mock_get)

    result = secrets.resolve_openbao_ref("openbao://kv/myapp#api_key")
    assert result == "key-abc-123"


def test_resolve_openbao_ref_with_namespace(monkeypatch):
    """Test resolving OpenBao reference with namespace."""
    monkeypatch.setenv("OPENBAO_ADDR", "https://vault.test.local:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")
    monkeypatch.setenv("OPENBAO_NAMESPACE", "my-namespace")
    monkeypatch.setenv("OPENBAO_KV_VERSION", "2")

    captured_headers = {}

    def mock_get(url, headers=None, **kwargs):
        captured_headers.update(headers or {})
        return FakeHTTPXResponse(
            json_data={"data": {"data": {"secret": "ns-value"}}}
        )

    import httpx
    monkeypatch.setattr(httpx, "get", mock_get)

    result = secrets.resolve_openbao_ref("openbao://secret/data/app#secret")
    assert result == "ns-value"
    assert "X-Vault-Namespace" in captured_headers


def test_resolve_openbao_ref_missing_field_error(monkeypatch):
    """Test error when requested field is missing."""
    monkeypatch.setenv("OPENBAO_ADDR", "https://vault.test.local:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")
    monkeypatch.setenv("OPENBAO_KV_VERSION", "2")

    mock_response = FakeHTTPXResponse(
        json_data={
            "data": {
                "data": {
                    "other_field": "some-value",
                }
            }
        }
    )

    def mock_get(url, **kwargs):
        return mock_response

    import httpx
    monkeypatch.setattr(httpx, "get", mock_get)

    with pytest.raises(HTTPException) as exc_info:
        secrets.resolve_openbao_ref("openbao://secret/data/myapp#missing_field")
    assert exc_info.value.status_code == 500
