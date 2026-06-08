"""The Zabbix webhook endpoints mount with no router-level auth and mutate
state (alert records, device sync), so they must authenticate themselves via a
shared secret. These tests pin the fail-closed contract.
"""

import pytest
from fastapi import HTTPException

from app.api import zabbix_webhook
from app.services import zabbix as zabbix_service


def test_missing_token_config_fails_closed(monkeypatch):
    """Unconfigured secret -> 503, never silent acceptance of anonymous calls."""
    monkeypatch.setattr(zabbix_webhook, "get_zabbix_webhook_token", lambda: "")
    with pytest.raises(HTTPException) as exc:
        zabbix_webhook._require_zabbix_webhook_token("anything")
    assert exc.value.status_code == 503


def test_wrong_token_rejected(monkeypatch):
    monkeypatch.setattr(
        zabbix_webhook, "get_zabbix_webhook_token", lambda: "expected-secret"
    )
    with pytest.raises(HTTPException) as exc:
        zabbix_webhook._require_zabbix_webhook_token("wrong-secret")
    assert exc.value.status_code == 401


def test_missing_header_rejected(monkeypatch):
    monkeypatch.setattr(
        zabbix_webhook, "get_zabbix_webhook_token", lambda: "expected-secret"
    )
    with pytest.raises(HTTPException) as exc:
        zabbix_webhook._require_zabbix_webhook_token(None)
    assert exc.value.status_code == 401


def test_valid_token_accepted(monkeypatch):
    monkeypatch.setattr(
        zabbix_webhook, "get_zabbix_webhook_token", lambda: "expected-secret"
    )
    # Should not raise.
    zabbix_webhook._require_zabbix_webhook_token("expected-secret")


def test_get_token_resolves_from_env(monkeypatch):
    monkeypatch.delenv("ZABBIX_WEBHOOK_TOKEN_FILE", raising=False)
    monkeypatch.setenv("ZABBIX_WEBHOOK_TOKEN", "env-secret")
    assert zabbix_service.get_zabbix_webhook_token() == "env-secret"


def test_get_token_empty_when_unset(monkeypatch):
    monkeypatch.delenv("ZABBIX_WEBHOOK_TOKEN_FILE", raising=False)
    monkeypatch.delenv("ZABBIX_WEBHOOK_TOKEN", raising=False)
    # No env/file; OpenBao absent in tests -> empty (fail-closed upstream).
    assert zabbix_service.get_zabbix_webhook_token() == ""
