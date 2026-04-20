from __future__ import annotations


def test_acs_backend_registry_supports_non_genie_backends() -> None:
    from app.services.acs_client import (
        AcsBackend,
        create_acs_config_writer,
        create_acs_event_ingestor,
        create_acs_state_reader,
        register_acs_backend,
        registered_acs_backends,
    )

    class FakeWriter:
        pass

    class FakeReader:
        pass

    class FakeIngestor:
        pass

    class FakeClient:
        def __init__(self, base_url, *, timeout=30.0, headers=None):
            self.base_url = base_url
            self.timeout = timeout
            self.headers = headers

    register_acs_backend(
        "axiroscwmp",
        AcsBackend(
            create_client=FakeClient,
            create_config_writer=FakeWriter,
            create_state_reader=FakeReader,
            create_event_ingestor=FakeIngestor,
        ),
        aliases=("axiros",),
    )

    assert "axiroscwmp" in registered_acs_backends()
    assert isinstance(create_acs_config_writer("axiros"), FakeWriter)
    assert isinstance(create_acs_state_reader("axiroscwmp"), FakeReader)
    assert isinstance(create_acs_event_ingestor("axiros"), FakeIngestor)


def test_acs_adapter_factories_return_genieacs_implementations() -> None:
    from app.services.acs_client import (
        create_acs_config_writer,
        create_acs_event_ingestor,
        create_acs_state_reader,
    )
    from app.services.acs_config_adapter import GenieAcsConfigWriter
    from app.services.acs_event_adapter import GenieAcsEventIngestor
    from app.services.acs_state_adapter import GenieAcsStateReader

    assert isinstance(create_acs_config_writer(), GenieAcsConfigWriter)
    assert isinstance(create_acs_state_reader(), GenieAcsStateReader)
    assert isinstance(create_acs_event_ingestor(), GenieAcsEventIngestor)


def test_acs_event_ingestor_delegates_inform(monkeypatch) -> None:
    from app.services import tr069 as tr069_service
    from app.services.acs_event_adapter import GenieAcsEventIngestor

    calls = {}

    def fake_receive_inform(db, **kwargs):
        calls["db"] = db
        calls["kwargs"] = kwargs
        return {"status": "ok", "source": "fake"}

    monkeypatch.setattr(tr069_service, "receive_inform", fake_receive_inform)

    result = GenieAcsEventIngestor().receive_inform(
        object(),
        serial_number="ABC123",
        device_id_raw="OUI-Model-ABC123",
        event="periodic",
        raw_payload={"serial": "ABC123"},
        request_id="req-1",
    )

    assert result == {"status": "ok", "source": "fake"}
    assert calls["kwargs"]["serial_number"] == "ABC123"
    assert calls["kwargs"]["device_id_raw"] == "OUI-Model-ABC123"
    assert calls["kwargs"]["request_id"] == "req-1"


def test_tr069_inform_route_uses_event_ingestor_factory(monkeypatch) -> None:
    from app.api import tr069_inform

    calls = {}

    class FakeIngestor:
        def receive_inform(self, db, **kwargs):
            calls["db"] = db
            calls["kwargs"] = kwargs
            return {"status": "ok", "source": "factory"}

    class FakeClient:
        host = "192.0.2.10"

    class FakeRequest:
        client = FakeClient()
        headers = {
            "x-request-id": "req-route",
            "user-agent": "pytest",
        }

    monkeypatch.setattr(
        tr069_inform,
        "create_acs_event_ingestor",
        lambda: FakeIngestor(),
    )

    payload = tr069_inform.InformPayload(
        serial_number="ABC123",
        device_id="OUI-Model-ABC123",
        event="periodic",
    )
    result = tr069_inform.receive_inform(FakeRequest(), payload, object())

    assert result == {"status": "ok", "source": "factory"}
    assert calls["kwargs"]["serial_number"] == "ABC123"
    assert calls["kwargs"]["device_id_raw"] == "OUI-Model-ABC123"
    assert calls["kwargs"]["request_id"] == "req-route"


def test_acs_config_adapter_delegates_wifi_config(monkeypatch) -> None:
    from app.services.acs_config_adapter import acs_config_adapter
    from app.services.network import ont_action_wifi
    from app.services.network.ont_action_common import ActionResult

    calls = {}

    def fake_set_wifi_config(db, ont_id, **kwargs):
        calls["db"] = db
        calls["ont_id"] = ont_id
        calls["kwargs"] = kwargs
        return ActionResult(success=True, message="ok", data={"adapter": "acs"})

    monkeypatch.setattr(ont_action_wifi, "set_wifi_config", fake_set_wifi_config)

    result = acs_config_adapter.set_wifi_config(
        object(),
        "ont-1",
        enabled=True,
        ssid="DOTMAC-1001",
        password="Secret123",
        channel=6,
        security_mode="WPA2-Personal",
    )

    assert result.success is True
    assert calls["ont_id"] == "ont-1"
    assert calls["kwargs"] == {
        "enabled": True,
        "ssid": "DOTMAC-1001",
        "password": "Secret123",
        "channel": 6,
        "security_mode": "WPA2-Personal",
    }


def test_acs_config_adapter_push_config_urgent_uses_verified_connection_request(
    monkeypatch,
) -> None:
    from app.services.acs_config_adapter import acs_config_adapter
    from app.services.network import ont_action_common

    calls = {}

    class FakeClient:
        pass

    def fake_get_ont_client_or_error(db, ont_id):
        calls["db"] = db
        calls["ont_id"] = ont_id
        return (object(), FakeClient(), "device-1"), None

    def fake_set_and_verify(
        client,
        device_id,
        parameters,
        *,
        expected=None,
        connection_request_attempts=3,
        connection_request_backoff_sec=1.0,
    ):
        calls["client"] = client
        calls["device_id"] = device_id
        calls["parameters"] = parameters
        calls["expected"] = expected
        calls["connection_request_attempts"] = connection_request_attempts
        calls["connection_request_backoff_sec"] = connection_request_backoff_sec
        return {"_id": "task-1"}

    monkeypatch.setattr(
        ont_action_common,
        "get_ont_client_or_error",
        fake_get_ont_client_or_error,
    )
    monkeypatch.setattr(ont_action_common, "set_and_verify", fake_set_and_verify)

    result = acs_config_adapter.push_config_urgent(
        object(),
        "ont-1",
        {"Device.WiFi.SSID.1.SSID": "DOTMAC"},
        expected={"Device.WiFi.SSID.1.SSID": "DOTMAC"},
    )

    assert result.success is True
    assert result.data["device_id"] == "device-1"
    assert result.data["connection_request"] is True
    assert calls["ont_id"] == "ont-1"
    assert calls["parameters"] == {"Device.WiFi.SSID.1.SSID": "DOTMAC"}
    assert calls["expected"] == {"Device.WiFi.SSID.1.SSID": "DOTMAC"}
    assert calls["connection_request_attempts"] == 3
    assert calls["connection_request_backoff_sec"] == 1.0


def test_verified_write_retries_connection_request_and_keeps_spv_on_success(
    monkeypatch,
) -> None:
    from app.services.network.ont_action_common import set_and_verify

    calls = {"gpv": 0, "deleted": []}

    class FakeClient:
        def set_parameter_values(self, device_id, params, connection_request=True):
            assert connection_request is False
            calls["spv"] = (device_id, params)
            return {"_id": "spv-task"}

        def get_parameter_values(self, device_id, paths, connection_request=True):
            assert connection_request is True
            calls["gpv"] += 1
            if calls["gpv"] < 3:
                return {
                    "_id": f"gpv-task-{calls['gpv']}",
                    "connectionRequestError": "Device is offline",
                }
            return {"_id": "gpv-task-3"}

        def get_device(self, _device_id):
            return {
                "Device": {
                    "WiFi": {
                        "SSID": {
                            "1": {
                                "SSID": {
                                    "_value": "DOTMAC",
                                    "_timestamp": "2026-04-19T00:00:00Z",
                                }
                            }
                        }
                    }
                }
            }

        def delete_task(self, task_id):
            calls["deleted"].append(task_id)

    monkeypatch.setattr("app.services.network.ont_action_common.time.sleep", lambda _: None)

    result = set_and_verify(
        FakeClient(),
        "device-1",
        {"Device.WiFi.SSID.1.SSID": "DOTMAC"},
        connection_request_attempts=3,
    )

    assert result == {"_id": "spv-task"}
    assert calls["gpv"] == 3
    assert calls["deleted"] == ["gpv-task-1", "gpv-task-2"]


def test_verified_write_deletes_queued_spv_when_connection_request_never_recovers(
    monkeypatch,
) -> None:
    import pytest

    from app.services.genieacs import GenieACSError
    from app.services.network.ont_action_common import set_and_verify

    deleted = []

    class FakeClient:
        def set_parameter_values(self, _device_id, _params, connection_request=True):
            assert connection_request is False
            return {"_id": "spv-task"}

        def get_parameter_values(self, _device_id, _paths, connection_request=True):
            assert connection_request is True
            return {
                "_id": "gpv-task",
                "connectionRequestError": "EHOSTUNREACH",
            }

        def delete_task(self, task_id):
            deleted.append(task_id)

    monkeypatch.setattr("app.services.network.ont_action_common.time.sleep", lambda _: None)

    with pytest.raises(GenieACSError, match="Connection request failed after 2 attempts"):
        set_and_verify(
            FakeClient(),
            "device-1",
            {"Device.WiFi.SSID.1.SSID": "DOTMAC"},
            connection_request_attempts=2,
        )

    assert deleted == ["gpv-task", "gpv-task", "spv-task"]


def test_acs_client_pool_falls_back_on_unavailable_primary() -> None:
    from app.services.acs_client import AcsClientPool
    from app.services.genieacs import GenieACSError

    class Primary:
        def list_devices(self, **_kwargs):
            raise GenieACSError("Request error: connection refused")

    class Secondary:
        def list_devices(self, **_kwargs):
            return [{"_id": "device-1"}]

    pool = AcsClientPool(Primary(), Secondary())

    assert pool.list_devices() == [{"_id": "device-1"}]


def test_acs_config_adapter_download_uses_acs_download_rpc(monkeypatch) -> None:
    from app.services.acs_config_adapter import acs_config_adapter
    from app.services.network import ont_action_common

    calls = {}

    class FakeOnt:
        serial_number = "ONT123"

    class FakeClient:
        def download(self, device_id, **kwargs):
            calls["device_id"] = device_id
            calls["kwargs"] = kwargs
            return {"_id": "download-task"}

    def fake_get_ont_client_or_error(db, ont_id):
        calls["db"] = db
        calls["ont_id"] = ont_id
        return (FakeOnt(), FakeClient(), "device-1"), None

    monkeypatch.setattr(
        ont_action_common,
        "get_ont_client_or_error",
        fake_get_ont_client_or_error,
    )

    result = acs_config_adapter.download(
        object(),
        "ont-1",
        file_type="1 Firmware Upgrade Image",
        file_url="https://example.test/fw.bin",
        filename="fw.bin",
    )

    assert result.success is True
    assert result.data["task"] == {"_id": "download-task"}
    assert calls["device_id"] == "device-1"
    assert calls["kwargs"] == {
        "file_type": "1 Firmware Upgrade Image",
        "file_url": "https://example.test/fw.bin",
        "filename": "fw.bin",
        "connection_request": True,
    }


def test_acs_config_adapter_firmware_upgrade_uses_firmware_image(
    db_session, monkeypatch
) -> None:
    from app.models.network import OntFirmwareImage
    from app.services.acs_config_adapter import acs_config_adapter
    from app.services.network import ont_action_common

    firmware = OntFirmwareImage(
        vendor="Huawei",
        model="EG8145V5",
        version="V1R2",
        file_url="https://example.test/eg8145v5.bin",
        filename="eg8145v5.bin",
        checksum="sha256:abc",
        file_size_bytes=1234,
        is_active=True,
    )
    db_session.add(firmware)
    db_session.commit()
    db_session.refresh(firmware)

    class FakeOnt:
        serial_number = "ONT123"

    class FakeClient:
        def download(self, _device_id, **_kwargs):
            return {"_id": "firmware-task"}

    monkeypatch.setattr(
        ont_action_common,
        "get_ont_client_or_error",
        lambda _db, _ont_id: ((FakeOnt(), FakeClient(), "device-1"), None),
    )

    result = acs_config_adapter.firmware_upgrade(
        db_session,
        "ont-1",
        str(firmware.id),
    )

    assert result.success is True
    assert result.data["firmware_image_id"] == str(firmware.id)
    assert result.data["firmware_version"] == "V1R2"
    assert result.data["task"] == {"_id": "firmware-task"}


def test_acs_config_adapter_delegates_wan_config(monkeypatch) -> None:
    from app.services.acs_config_adapter import acs_config_adapter
    from app.services.network import ont_action_network
    from app.services.network.ont_action_common import ActionResult

    calls = {}

    def fake_configure_wan_config(db, ont_id, **kwargs):
        calls["db"] = db
        calls["ont_id"] = ont_id
        calls["kwargs"] = kwargs
        return ActionResult(success=True, message="ok", data={"adapter": "acs"})

    monkeypatch.setattr(
        ont_action_network,
        "configure_wan_config",
        fake_configure_wan_config,
    )

    result = acs_config_adapter.configure_wan_config(
        object(),
        "ont-1",
        wan_mode="static",
        wan_vlan=203,
        ip_address="100.64.1.20",
        subnet_mask="255.255.255.0",
        gateway="100.64.1.1",
        dns_servers="1.1.1.1",
        instance_index=2,
    )

    assert result.success is True
    assert calls["ont_id"] == "ont-1"
    assert calls["kwargs"] == {
        "wan_mode": "static",
        "wan_vlan": 203,
        "ip_address": "100.64.1.20",
        "subnet_mask": "255.255.255.0",
        "gateway": "100.64.1.1",
        "dns_servers": "1.1.1.1",
        "instance_index": 2,
    }


def test_acs_config_adapter_queues_wifi_config(monkeypatch) -> None:
    from app.services.acs_config_adapter import acs_config_adapter
    from app.services.queue_adapter import QueueDispatchResult

    calls = {}

    def fake_enqueue_task(task_name, **kwargs):
        calls["task_name"] = task_name
        calls["kwargs"] = kwargs
        return QueueDispatchResult(
            queued=True,
            task_id="task-1",
            task_name=task_name,
            queue=kwargs.get("queue"),
        )

    monkeypatch.setattr("app.services.queue_adapter.enqueue_task", fake_enqueue_task)

    result = acs_config_adapter.queue_set_wifi_config(
        object(),
        "ont-1",
        enabled=True,
        ssid="DOTMAC-1001",
        password="Secret123",
        channel=6,
        security_mode="WPA2-Personal",
        actor_id="admin-1",
    )

    assert result.queued is True
    assert result.task_id == "task-1"
    assert result.queue == "acs"
    assert calls == {
        "task_name": "app.tasks.tr069.apply_acs_config",
        "kwargs": {
            "args": ("set_wifi_config", "ont-1"),
            "kwargs": {
                "args": [],
                "kwargs": {
                    "enabled": True,
                    "ssid": "DOTMAC-1001",
                    "password": "Secret123",
                    "channel": 6,
                    "security_mode": "WPA2-Personal",
                },
            },
            "queue": "acs",
            "correlation_id": "acs_config:ont-1:set_wifi_config",
            "source": "acs_config_adapter",
            "request_id": None,
            "actor_id": "admin-1",
        },
    }


def test_acs_config_adapter_rejects_unknown_queue_action() -> None:
    from app.services.acs_config_adapter import acs_config_adapter

    result = acs_config_adapter.queue_config_action(
        object(),
        "delete_everything",
        "ont-1",
    )

    assert result.queued is False
    assert "Unsupported ACS configuration action" in str(result.error)


def test_apply_acs_config_task_executes_adapter_method(monkeypatch) -> None:
    from app.services import acs_client
    from app.services.network.ont_action_common import ActionResult
    from app.tasks import tr069

    class FakeSession:
        committed = False
        rolled_back = False
        closed = False

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    session = FakeSession()
    calls = {}

    class FakeWriter:
        @property
        def queueable_actions(self):
            return frozenset({"set_wifi_ssid"})

        def supports_config_action(self, action):
            calls["supports_action"] = action
            return action in self.queueable_actions

        def execute_config_action(self, db, action, ont_id, *, args=None, kwargs=None):
            calls["execute"] = {
                "db": db,
                "action": action,
                "ont_id": ont_id,
                "args": args,
                "kwargs": kwargs,
            }
            return ActionResult(
                success=True,
                message="queued apply ok",
                data={"x": 1},
            )

    def fake_session_local():
        return session

    monkeypatch.setattr(tr069, "SessionLocal", fake_session_local)
    monkeypatch.setattr(acs_client, "create_acs_config_writer", lambda: FakeWriter())

    result = tr069.apply_acs_config.run(
        "set_wifi_ssid",
        "ont-1",
        args=["DOTMAC"],
    )

    assert result == {
        "action": "set_wifi_ssid",
        "ont_id": "ont-1",
        "success": True,
        "message": "queued apply ok",
        "waiting": False,
        "data": {"x": 1},
    }
    assert calls == {
        "supports_action": "set_wifi_ssid",
        "execute": {
            "db": session,
            "action": "set_wifi_ssid",
            "ont_id": "ont-1",
            "args": ["DOTMAC"],
            "kwargs": None,
        },
    }
    assert session.committed is True
    assert session.rolled_back is False
    assert session.closed is True


def test_web_ont_config_writer_is_resolved_per_call(monkeypatch) -> None:
    from app.services.network.ont_action_common import ActionResult
    from app.services.web_network_ont_actions import config_setters

    calls = []

    class FakeWriter:
        def __init__(self, label):
            self.label = label

        def set_wifi_ssid(self, db, ont_id, ssid):
            calls.append((self.label, ont_id, ssid))
            return ActionResult(success=False, message=self.label)

    writers = iter([FakeWriter("first"), FakeWriter("second")])
    monkeypatch.setattr(
        config_setters, "create_acs_config_writer", lambda: next(writers)
    )

    config_setters.set_wifi_ssid(object(), "ont-1", "SSID-1")
    config_setters.set_wifi_ssid(object(), "ont-2", "SSID-2")

    assert calls == [
        ("first", "ont-1", "SSID-1"),
        ("second", "ont-2", "SSID-2"),
    ]
