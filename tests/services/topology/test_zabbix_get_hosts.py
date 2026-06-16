"""get_hosts payload shape for the topology reconcile (Phase 1, Task 2)."""

from __future__ import annotations

from app.services.zabbix import ALLOWED_METHODS, ZabbixClient


def _client_capturing_payload():
    client = ZabbixClient(api_url="http://zabbix/api", api_token="tok")
    captured: dict = {}

    def _fake_submit(payload, expected):
        captured["payload"] = payload
        captured["expected"] = expected
        return []

    client._submit_read_payload = _fake_submit  # type: ignore[method-assign]
    return client, captured


def test_host_get_is_allowed():
    assert "host.get" in ALLOWED_METHODS


def test_get_hosts_sets_groupids_when_group_ids_passed():
    client, captured = _client_capturing_payload()
    client.get_hosts(group_ids=["10", "11"])
    params = captured["payload"]["params"]
    assert captured["payload"]["method"] == "host.get"
    assert captured["expected"] == "host.get"
    assert params["groupids"] == ["10", "11"]
    # Still selects the structure the reconcile needs.
    assert "selectGroups" in params
    assert "selectInterfaces" in params


def test_get_hosts_omits_groupids_when_not_passed():
    client, captured = _client_capturing_payload()
    client.get_hosts()
    assert "groupids" not in captured["payload"]["params"]
