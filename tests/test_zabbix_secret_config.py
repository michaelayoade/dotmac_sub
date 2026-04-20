from __future__ import annotations

from types import SimpleNamespace

from app.services import zabbix


def test_zabbix_token_resolves_secret_reference(monkeypatch):
    monkeypatch.setenv("ZABBIX_API_TOKEN", "bao://secret/zabbix#api_token")
    monkeypatch.setattr("app.services.secrets.get_secret", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        "app.services.secrets.resolve_secret",
        lambda value: "resolved-token" if value == "bao://secret/zabbix#api_token" else value,
    )

    assert zabbix.get_zabbix_api_token() == "resolved-token"


def test_zabbix_token_prefers_openbao_over_env(monkeypatch):
    monkeypatch.setenv("ZABBIX_API_TOKEN", "env-token")
    monkeypatch.setattr(
        "app.services.secrets.get_secret",
        lambda path, field, default="": "bao-token"
        if (path, field) == ("zabbix", "api_token")
        else default,
    )

    assert zabbix.get_zabbix_api_token() == "bao-token"


def test_zabbix_token_file_fallback(monkeypatch, tmp_path):
    token_file = tmp_path / "zabbix-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.delenv("ZABBIX_API_TOKEN", raising=False)
    monkeypatch.setattr("app.services.secrets.get_secret", lambda *_args, **_kwargs: "")
    monkeypatch.setenv("ZABBIX_API_TOKEN_FILE", str(token_file))

    assert zabbix.get_zabbix_api_token() == "file-token"


def test_zabbix_configured_uses_resolved_token(monkeypatch):
    monkeypatch.setattr(zabbix, "get_zabbix_api_url", lambda: "http://zabbix/api")
    monkeypatch.setattr(zabbix, "get_zabbix_api_token", lambda: "resolved-token")

    assert zabbix.zabbix_configured() is True


def test_zabbix_availability_success(monkeypatch):
    monkeypatch.setattr(zabbix, "get_zabbix_api_url", lambda: "http://zabbix/api")
    monkeypatch.setattr(zabbix, "get_zabbix_api_token", lambda: "resolved-token")
    monkeypatch.setattr(zabbix.ZabbixClient, "get_hosts", lambda self, limit=1: [])

    health = zabbix.check_zabbix_availability(timeout=0.1)

    assert health == {
        "configured": True,
        "available": True,
        "status": "up",
        "api_url": "http://zabbix/api",
    }


def test_zabbix_availability_missing_token(monkeypatch):
    monkeypatch.setattr(zabbix, "get_zabbix_api_url", lambda: "http://zabbix/api")
    monkeypatch.setattr(zabbix, "get_zabbix_api_token", lambda: "")

    health = zabbix.check_zabbix_availability(timeout=0.1)

    assert health["configured"] is False
    assert health["available"] is False
    assert health["status"] == "not_configured"
    assert "token" in health["message"].lower()


def test_zabbix_metrics_adapter_uses_shared_secret_resolvers(monkeypatch):
    from app.services.network.metrics_adapters import ZabbixMetricsAdapter

    monkeypatch.setattr(zabbix, "get_zabbix_api_url", lambda: "http://zabbix/api")
    monkeypatch.setattr(zabbix, "get_zabbix_api_token", lambda: "resolved-token")

    adapter = ZabbixMetricsAdapter()

    assert adapter.api_url == "http://zabbix/api"
    assert adapter.api_token == "resolved-token"


def test_zabbix_device_sync_rolls_back_when_api_unavailable(monkeypatch):
    from app.tasks import zabbix_ingestion
    from app.services import zabbix_host_sync

    calls: list[str] = []
    db = SimpleNamespace(
        commit=lambda: calls.append("commit"),
        rollback=lambda: calls.append("rollback"),
        close=lambda: calls.append("close"),
    )

    def raise_unavailable(_db):
        raise zabbix.ZabbixClientError("zabbix down")

    monkeypatch.setattr(zabbix_ingestion, "_zabbix_enabled", lambda: True)
    monkeypatch.setattr(
        zabbix_ingestion.db_session_adapter,
        "create_session",
        lambda: db,
    )
    monkeypatch.setattr(zabbix_host_sync, "sync_all_devices", raise_unavailable)

    result = zabbix_ingestion.sync_devices_to_zabbix.run()

    assert result == {"error": "zabbix_unavailable", "message": "zabbix down"}
    assert calls == ["rollback", "close"]
