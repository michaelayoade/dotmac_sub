from app.services.network.ont_action_common import (
    TR069_ROOT_DEVICE,
    TR069_ROOT_IGD,
    resolve_wan_ppp_instance,
)


def _device_from_values(values: dict[str, object]) -> dict:
    doc: dict = {}
    for path, value in values.items():
        node = doc
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = {"_value": value}
    return doc


class FakeClient:
    def __init__(self, device: dict | None = None, exc: Exception | None = None):
        self.device = device or {}
        self.exc = exc

    def get_device(self, _device_id: str):
        if self.exc is not None:
            raise self.exc
        return self.device


def test_resolve_wan_ppp_instance_discovers_ppp_slot_two() -> None:
    device = _device_from_values(
        {
            "InternetGatewayDevice.WANDevice.1.WANConnectionDeviceNumberOfEntries": 1,
            (
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1."
                "WANPPPConnectionNumberOfEntries"
            ): 0,
            (
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2."
                "WANPPPConnectionNumberOfEntries"
            ): 1,
        }
    )

    assert resolve_wan_ppp_instance(FakeClient(device), "device-1", TR069_ROOT_IGD) == 2


def test_resolve_wan_ppp_instance_falls_back_for_tr181_or_fetch_failure() -> None:
    assert (
        resolve_wan_ppp_instance(FakeClient(), "device-1", TR069_ROOT_DEVICE, default=3)
        == 3
    )
    assert (
        resolve_wan_ppp_instance(
            FakeClient(exc=RuntimeError("offline")),
            "device-1",
            TR069_ROOT_IGD,
            default=4,
        )
        == 4
    )
