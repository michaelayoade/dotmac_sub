from __future__ import annotations


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
    from app.services.acs_config_adapter import acs_config_adapter
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

    def fake_session_local():
        return session

    def fake_set_wifi_ssid(db, ont_id, ssid):
        calls["db"] = db
        calls["ont_id"] = ont_id
        calls["ssid"] = ssid
        return ActionResult(success=True, message="queued apply ok", data={"x": 1})

    monkeypatch.setattr(tr069, "SessionLocal", fake_session_local)
    monkeypatch.setattr(acs_config_adapter, "set_wifi_ssid", fake_set_wifi_ssid)

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
    assert calls == {"db": session, "ont_id": "ont-1", "ssid": "DOTMAC"}
    assert session.committed is True
    assert session.rolled_back is False
    assert session.closed is True
