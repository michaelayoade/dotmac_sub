"""NetworkDevice observed-status gate (#44).

DeviceStatus mixes poller-observed states (online/offline/degraded) with the
operator-set admin state (maintenance) in one column. All poller writes go
through set_device_observed_status, which must never overwrite maintenance.
"""

from app.models.network_monitoring import DeviceStatus, NetworkDevice
from app.services.web_network_core_runtime import set_device_observed_status


def _device(status: DeviceStatus) -> NetworkDevice:
    device = NetworkDevice()
    device.status = status
    return device


def test_observed_status_applies_when_not_maintenance():
    device = _device(DeviceStatus.online)
    assert set_device_observed_status(device, DeviceStatus.offline) is True
    assert device.status == DeviceStatus.offline


def test_maintenance_is_never_overwritten_by_poller():
    device = _device(DeviceStatus.maintenance)
    for observed in (DeviceStatus.online, DeviceStatus.offline, DeviceStatus.degraded):
        assert set_device_observed_status(device, observed) is False
        assert device.status == DeviceStatus.maintenance


def test_same_status_is_noop():
    device = _device(DeviceStatus.online)
    assert set_device_observed_status(device, DeviceStatus.online) is False
    assert device.status == DeviceStatus.online
