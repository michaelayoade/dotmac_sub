"""Contract tests for the core router live-bandwidth service (VictoriaMetrics)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services import core_router_metrics
from app.services.core_router_metrics import (
    CoreRouterBandwidth,
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


def _vm_response(series: list[dict]):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": "success", "data": {"result": series}}
    return response


def _series(interface_id, bps: float, clock: int = 1_700_000_200):
    return {
        "metric": {"interface_id": str(interface_id)},
        "value": [clock, str(bps)],
    }


@pytest.fixture(autouse=True)
def _clear_caches():
    core_router_metrics._bandwidth_cache.clear()
    yield
    core_router_metrics._bandwidth_cache.clear()


def test_no_monitored_interfaces_returns_empty_without_querying():
    device = _make_device()
    interfaces = [_make_iface("eth1", 1, monitored=False)]
    with patch.object(core_router_metrics, "_get_client") as client:
        result = get_interface_bandwidth(MagicMock(), device, interfaces)
    client.assert_not_called()
    assert result.by_interface_id == {}
    assert result.error is None


def test_returns_rx_tx_for_monitored_interfaces():
    device = _make_device()
    iface = _make_iface("sfp-sfpplus1", 5)
    fake_client = MagicMock()
    fake_client.get.side_effect = [
        _vm_response([_series(iface.id, 3_200_000.0, clock=1_700_000_200)]),
        _vm_response([_series(iface.id, 420_000.0, clock=1_700_000_201)]),
    ]
    with patch.object(core_router_metrics, "_get_client", return_value=fake_client):
        result = get_interface_bandwidth(MagicMock(), device, [iface])
    assert result.error is None
    assert fake_client.get.call_count == 2
    query = fake_client.get.call_args_list[0].kwargs["params"]["query"]
    assert f'device_id="{device.id}"' in query
    assert "rate(core_interface_in_octets_total" in query
    bw = result.by_interface_id[str(iface.id)]
    assert bw.rx_bps == 3_200_000.0
    assert bw.tx_bps == 420_000.0
    assert bw.last_clock == 1_700_000_201


def test_interface_without_series_is_absent_from_result():
    device = _make_device()
    with_data = _make_iface("sfp1", 5)
    without_data = _make_iface("sfp2", 7)
    fake_client = MagicMock()
    fake_client.get.side_effect = [
        _vm_response([_series(with_data.id, 1000.0)]),
        _vm_response([_series(with_data.id, 2000.0)]),
    ]
    with patch.object(core_router_metrics, "_get_client", return_value=fake_client):
        result = get_interface_bandwidth(MagicMock(), device, [with_data, without_data])
    assert str(with_data.id) in result.by_interface_id
    assert str(without_data.id) not in result.by_interface_id
    assert result.error is None


def test_no_series_at_all_returns_explanatory_error():
    device = _make_device()
    iface = _make_iface("eth1", 1)
    fake_client = MagicMock()
    fake_client.get.side_effect = [_vm_response([]), _vm_response([])]
    with patch.object(core_router_metrics, "_get_client", return_value=fake_client):
        result = get_interface_bandwidth(MagicMock(), device, [iface])
    assert result.error == "Interface counters not enabled in monitoring"
    assert result.by_interface_id == {}


def test_vm_status_error_renders_as_unavailable():
    device = _make_device()
    iface = _make_iface("eth1", 1)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": "error", "error": "cannot parse query"}
    fake_client = MagicMock()
    fake_client.get.return_value = response
    with patch.object(core_router_metrics, "_get_client", return_value=fake_client):
        result = get_interface_bandwidth(MagicMock(), device, [iface])
    assert result.error == "Live monitoring unavailable"
    assert result.by_interface_id == {}


def test_malformed_json_renders_as_unavailable():
    device = _make_device()
    iface = _make_iface("eth1", 1)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.side_effect = ValueError("not json")
    fake_client = MagicMock()
    fake_client.get.return_value = response
    with patch.object(core_router_metrics, "_get_client", return_value=fake_client):
        result = get_interface_bandwidth(MagicMock(), device, [iface])
    assert result.error == "Live monitoring unavailable"
    assert result.by_interface_id == {}


def test_query_failure_renders_as_unavailable():
    device = _make_device()
    iface = _make_iface("eth1", 1)
    fake_client = MagicMock()
    fake_client.get.side_effect = httpx.ConnectError("connection refused")
    with patch.object(core_router_metrics, "_get_client", return_value=fake_client):
        result = get_interface_bandwidth(MagicMock(), device, [iface])
    assert result.error == "Live monitoring unavailable"
    assert result.by_interface_id == {}


def test_result_is_cached_per_device():
    device = _make_device()
    iface = _make_iface("eth1", 5)
    fake_client = MagicMock()
    fake_client.get.side_effect = [
        _vm_response([_series(iface.id, 1000.0)]),
        _vm_response([_series(iface.id, 2000.0)]),
    ]
    with patch.object(core_router_metrics, "_get_client", return_value=fake_client):
        first = get_interface_bandwidth(MagicMock(), device, [iface])
        second = get_interface_bandwidth(MagicMock(), device, [iface])
    assert fake_client.get.call_count == 2  # one rx + one tx, second call cached
    assert second is first


def test_cache_invalidate_drops_entry():
    device = _make_device()
    core_router_metrics._bandwidth_cache[str(device.id)] = (
        CoreRouterBandwidth(by_interface_id={}, fetched_at=0.0),
        0.0,
    )
    core_router_metrics.invalidate_cache(device.id)
    assert str(device.id) not in core_router_metrics._bandwidth_cache
