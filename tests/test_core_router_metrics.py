"""Contract tests for the core router live-bandwidth service."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import core_router_metrics
from app.services.core_router_metrics import (
    CoreRouterBandwidth,
    _parse_items_by_snmp_index,
    get_interface_bandwidth,
)


def _make_iface(name: str, snmp_index: int, monitored: bool = True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        snmp_index=snmp_index,
        monitored=monitored,
    )


def _make_device(name: str = "Abuja Core | Garki", mgmt_ip: str = "10.0.0.1"):
    return SimpleNamespace(id=uuid.uuid4(), name=name, mgmt_ip=mgmt_ip)


def _make_db_with_nas(zabbix_host_id: str | None = "10692"):
    """Return a fake Session whose scalars().first() yields a NAS row with the given zabbix id."""
    nas = SimpleNamespace(zabbix_host_id=zabbix_host_id) if zabbix_host_id else None
    db = MagicMock()
    db.scalars.return_value.first.return_value = nas
    return db


@pytest.fixture(autouse=True)
def _clear_caches():
    core_router_metrics._bandwidth_cache.clear()
    yield
    core_router_metrics._bandwidth_cache.clear()


def test_parse_items_groups_in_out_by_snmp_index():
    items = [
        {
            "key_": "net.if.in[ifHCInOctets.5]",
            "lastvalue": "1500000",
            "lastclock": "1700000100",
        },
        {
            "key_": "net.if.out[ifHCOutOctets.5]",
            "lastvalue": "900000",
            "lastclock": "1700000101",
        },
        {
            "key_": "net.if.in[ifHCInOctets.7]",
            "lastvalue": "0",
            "lastclock": "1700000050",
        },
        {"key_": "unrelated.key", "lastvalue": "x", "lastclock": "1700000000"},
    ]
    grouped = _parse_items_by_snmp_index(items)
    assert grouped[5]["in"] == (1500000.0, 1700000100)
    assert grouped[5]["out"] == (900000.0, 1700000101)
    assert grouped[7] == {"in": (0.0, 1700000050)}
    assert 999 not in grouped


def test_parse_items_skips_non_numeric_values():
    items = [
        {"key_": "net.if.in[ifHCInOctets.1]", "lastvalue": None, "lastclock": "1700"},
        {
            "key_": "net.if.out[ifHCOutOctets.1]",
            "lastvalue": "garbage",
            "lastclock": "1700",
        },
    ]
    grouped = _parse_items_by_snmp_index(items)
    # value 0 stored from None; "garbage" raises ValueError → dropped
    assert grouped[1].get("in") == (0.0, 1700)
    assert "out" not in grouped[1]


def test_no_monitored_interfaces_returns_empty_without_calling_zabbix():
    device = _make_device()
    db = _make_db_with_nas()
    interfaces = [_make_iface("eth1", 1, monitored=False)]
    with patch.object(core_router_metrics, "ZabbixClient") as zclient:
        result = get_interface_bandwidth(db, device, interfaces)
    zclient.from_env.assert_not_called()
    # Also no DB query — short-circuits before resolver
    db.scalars.assert_not_called()
    assert result.by_interface_id == {}
    assert result.error is None


def test_returns_rx_tx_for_monitored_interfaces():
    device = _make_device()
    db = _make_db_with_nas(zabbix_host_id="10692")
    iface = _make_iface("sfp-sfpplus1", 5)
    fake_client = MagicMock()
    fake_client.get_items.return_value = [
        {
            "key_": "net.if.in[ifHCInOctets.5]",
            "lastvalue": "3200000",
            "lastclock": "1700000200",
        },
        {
            "key_": "net.if.out[ifHCOutOctets.5]",
            "lastvalue": "420000",
            "lastclock": "1700000201",
        },
    ]
    with patch.object(
        core_router_metrics.ZabbixClient, "from_env", return_value=fake_client
    ):
        result = get_interface_bandwidth(db, device, [iface])
    assert result.error is None
    fake_client.get_items.assert_called_once_with(host_ids=["10692"], metric="net.if")
    bw = result.by_interface_id[str(iface.id)]
    assert bw.rx_bps == 3200000.0
    assert bw.tx_bps == 420000.0
    assert bw.last_clock == 1700000201


def test_unlinked_device_returns_clear_error():
    device = _make_device("Garki Core (not yet synced)")
    db = _make_db_with_nas(zabbix_host_id=None)  # NAS exists, no Zabbix id yet
    iface = _make_iface("eth1", 1)
    with patch.object(core_router_metrics, "ZabbixClient") as zclient:
        result = get_interface_bandwidth(db, device, [iface])
    zclient.from_env.assert_not_called()
    assert result.error == "Device not linked to live monitoring"
    assert result.by_interface_id == {}


def test_no_matching_nas_returns_clear_error():
    device = _make_device()
    db = _make_db_with_nas(zabbix_host_id=None)
    db.scalars.return_value.first.return_value = None  # no NAS at all
    iface = _make_iface("eth1", 1)
    with patch.object(core_router_metrics, "ZabbixClient") as zclient:
        result = get_interface_bandwidth(db, device, [iface])
    zclient.from_env.assert_not_called()
    assert result.error == "Device not linked to live monitoring"


def test_zabbix_with_no_interface_items_returns_explanatory_error():
    device = _make_device()
    db = _make_db_with_nas("10958")
    iface = _make_iface("eth1", 1)
    fake_client = MagicMock()
    fake_client.get_items.return_value = [
        {"key_": "icmppingloss", "lastvalue": "0", "lastclock": "1700"},
        {"key_": "net.if.walk", "lastvalue": "", "lastclock": "1700"},
    ]
    with patch.object(
        core_router_metrics.ZabbixClient, "from_env", return_value=fake_client
    ):
        result = get_interface_bandwidth(db, device, [iface])
    assert result.error == "Interface counters not enabled in monitoring"
    assert result.by_interface_id == {}


def test_zabbix_client_error_renders_as_unreachable():
    from app.services.zabbix import ZabbixClientError

    device = _make_device()
    db = _make_db_with_nas("10692")
    iface = _make_iface("eth1", 1)
    fake_client = MagicMock()
    fake_client.get_items.side_effect = ZabbixClientError("network down")
    with patch.object(
        core_router_metrics.ZabbixClient, "from_env", return_value=fake_client
    ):
        result = get_interface_bandwidth(db, device, [iface])
    assert result.error == "Live monitoring unavailable"


def test_cache_invalidate_drops_entry():
    device = _make_device()
    core_router_metrics._bandwidth_cache[str(device.id)] = (
        CoreRouterBandwidth(by_interface_id={}, fetched_at=0.0),
        0.0,
    )
    core_router_metrics.invalidate_cache(device.id)
    assert str(device.id) not in core_router_metrics._bandwidth_cache
