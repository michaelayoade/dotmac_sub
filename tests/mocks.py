"""Mock utilities for testing external dependencies."""

from typing import Any
from unittest.mock import MagicMock


class FakeSMTP:
    """Mock SMTP server for email tests."""

    def __init__(self, host: str = "", port: int = 25, **kwargs):
        self.host = host
        self.port = port
        self.messages: list[tuple[str, list[str], str]] = []
        self.connected = False
        self.logged_in = False

    def __enter__(self):
        self.connected = True
        return self

    def __exit__(self, *args):
        self.connected = False

    def starttls(self):
        pass

    def login(self, user: str, password: str):
        self.logged_in = True

    def sendmail(self, from_addr: str, to_addrs: list[str], msg: str):
        self.messages.append((from_addr, to_addrs, msg))

    def quit(self):
        self.connected = False


class FakeHTTPXResponse:
    """Mock httpx response for API tests."""

    def __init__(self, json_data: dict | None = None, status_code: int = 200):
        self._json_data = json_data or {}
        self.status_code = status_code

    def json(self) -> dict:
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP Error: {self.status_code}")


class FakeHTTPXClient:
    """Mock httpx client for OpenBao/API tests."""

    def __init__(self, responses: dict[str, FakeHTTPXResponse] | None = None):
        self.responses = responses or {}
        self.requests: list[tuple[str, str, dict]] = []

    def get(self, url: str, **kwargs) -> FakeHTTPXResponse:
        self.requests.append(("GET", url, kwargs))
        return self.responses.get(url, FakeHTTPXResponse())

    def post(self, url: str, **kwargs) -> FakeHTTPXResponse:
        self.requests.append(("POST", url, kwargs))
        return self.responses.get(url, FakeHTTPXResponse())


class FakePyRadPacket:
    """Mock pyrad packet for RADIUS tests."""

    def __init__(self):
        self.code = 0
        self._attrs: dict[str, Any] = {}

    def __setitem__(self, key: str, value: Any):
        self._attrs[key] = value

    def __getitem__(self, key: str) -> Any:
        return self._attrs.get(key)


class FakePyRadClient:
    """Mock pyrad.Client for RADIUS tests."""

    # RADIUS response codes
    AccessAccept = 2
    AccessReject = 3

    def __init__(
        self,
        server: str = "",
        secret: bytes = b"",
        dict_path: str = "",
        should_accept: bool = True,
        should_timeout: bool = False,
    ):
        self.server = server
        self.secret = secret
        self.dict_path = dict_path
        self.should_accept = should_accept
        self.should_timeout = should_timeout
        self.auth_port = 1812
        self.acct_port = 1813
        self.timeout = 5
        self.retries = 3
        self.packets_sent: list[FakePyRadPacket] = []

    def CreateAuthPacket(self, **kwargs) -> FakePyRadPacket:
        packet = FakePyRadPacket()
        for key, value in kwargs.items():
            packet[key] = value
        return packet

    def SendPacket(self, packet: FakePyRadPacket) -> FakePyRadPacket:
        self.packets_sent.append(packet)
        if self.should_timeout:
            import socket
            raise socket.timeout("Connection timed out")
        response = FakePyRadPacket()
        response.code = self.AccessAccept if self.should_accept else self.AccessReject
        return response


class FakeGenieACSClient:
    """Mock GenieACS client for TR-069 tests."""

    def __init__(self, base_url: str = "", should_fail: bool = False):
        self.base_url = base_url
        self.should_fail = should_fail
        self.calls: list[tuple[str, dict]] = []

    def get_device(self, device_id: str) -> dict:
        self.calls.append(("get_device", {"device_id": device_id}))
        if self.should_fail:
            from app.services.genieacs import GenieACSError
            raise GenieACSError("Device not found")
        return {
            "DeviceId": device_id,
            "SerialNumber": "TEST123",
            "Manufacturer": "TestVendor",
        }

    def get_parameters(self, device_id: str, params: list[str]) -> dict:
        self.calls.append(("get_parameters", {"device_id": device_id, "params": params}))
        return {param: f"value_{param}" for param in params}

    def set_parameters(self, device_id: str, params: dict) -> bool:
        self.calls.append(("set_parameters", {"device_id": device_id, "params": params}))
        return not self.should_fail

    def reboot(self, device_id: str) -> bool:
        self.calls.append(("reboot", {"device_id": device_id}))
        return not self.should_fail

    def factory_reset(self, device_id: str) -> bool:
        self.calls.append(("factory_reset", {"device_id": device_id}))
        return not self.should_fail


class FakeProvisioner:
    """Mock provisioning adapter for provisioning tests."""

    def __init__(self, should_fail: bool = False, fail_step: int | None = None):
        self.should_fail = should_fail
        self.fail_step = fail_step
        self.calls: list[tuple[str, dict]] = []
        self.step_count = 0

    def assign_ont(self, **kwargs) -> dict:
        self.calls.append(("assign_ont", kwargs))
        return {"success": not self.should_fail, "ont_id": "ONT-001"}

    def push_config(self, **kwargs) -> dict:
        self.calls.append(("push_config", kwargs))
        self.step_count += 1
        if self.fail_step and self.step_count == self.fail_step:
            return {"success": False, "error": "Config push failed"}
        return {"success": not self.should_fail}

    def confirm_up(self, **kwargs) -> dict:
        self.calls.append(("confirm_up", kwargs))
        return {"success": not self.should_fail, "status": "up"}

    def deprovision(self, **kwargs) -> dict:
        self.calls.append(("deprovision", kwargs))
        return {"success": not self.should_fail}


class FakeRedis:
    """Mock Redis client for rate limiting and caching tests."""

    def __init__(self):
        self.store: dict[str, Any] = {}
        self.expiry: dict[str, int] = {}

    def get(self, key: str) -> Any:
        return self.store.get(key)

    def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.store[key] = value
        if ex:
            self.expiry[key] = ex
        return True

    def incr(self, key: str) -> int:
        self.store[key] = int(self.store.get(key, 0)) + 1
        return int(self.store[key])

    def expire(self, key: str, seconds: int) -> bool:
        self.expiry[key] = seconds
        return True

    def delete(self, key: str) -> int:
        if key in self.store:
            del self.store[key]
            return 1
        return 0

    def exists(self, key: str) -> int:
        return 1 if key in self.store else 0
