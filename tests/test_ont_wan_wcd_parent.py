"""WCD parent creation for TR-098 ONTs that only expose the management WCD."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.genieacs_client import GenieACSError
from app.services.network import ont_action_wan

ROOT = "InternetGatewayDevice"
WCD = f"{ROOT}.WANDevice.1.WANConnectionDevice."
MGMT_WCD = {
    "WANIPConnectionNumberOfEntries": {"_value": 1},
    "WANPPPConnectionNumberOfEntries": {"_value": 0},
    "WANIPConnection": {
        "1": {"X_HW_SERVICELIST": {"_value": "TR069"}, "X_HW_VLAN": {"_value": "201"}}
    },
}
BLANK_WCD = {
    "WANIPConnectionNumberOfEntries": {"_value": 0},
    "WANPPPConnectionNumberOfEntries": {"_value": 0},
}


def _doc(wcds: dict) -> dict:
    return {ROOT: {"WANDevice": {"1": {"WANConnectionDevice": wcds}}}}


class FakeClient:
    """Minimal ACS: addObject on the WCD parent materialises WCD2 (if visible)."""

    def __init__(self, wcds, *, parent_visible=True):
        self.wcds = wcds
        self.parent_visible = parent_visible
        self.added: list[str] = []

    def get_device(self, device_id):
        return _doc(self.wcds)

    def add_object(self, device_id, path):
        self.added.append(path)
        if path == WCD:
            if self.parent_visible:
                self.wcds["2"] = dict(BLANK_WCD)
        elif path.endswith(".WANPPPConnection."):
            wcd = path.split("WANConnectionDevice.")[1].split(".")[0]
            self.wcds[wcd]["WANPPPConnectionNumberOfEntries"] = {"_value": 1}
            self.wcds[wcd]["WANPPPConnection"] = {"1": {"Username": {"_value": ""}}}
        return {"_id": "t"}

    def refresh_object(self, device_id, path, **kwargs):
        return {"_id": "r"}

    def extract_parameter_value(self, device, path):
        node = device
        for part in path.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
            if node is None:
                return None
        if isinstance(node, dict):
            return node.get("_value")
        return node


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(writes=[], fail_on=None)
    ont = SimpleNamespace(serial_number="HG8546M-TEST", tr069_last_snapshot={})

    def install(client):
        monkeypatch.setattr(
            ont_action_wan,
            "get_ont_client_or_error",
            lambda db, ont_id: ((ont, client, "dev-1"), None),
        )
        return ont

    monkeypatch.setattr(ont_action_wan, "detect_data_model_root", lambda *a, **k: ROOT)
    monkeypatch.setattr(ont_action_wan, "persist_data_model_root", lambda *a: None)
    monkeypatch.setattr(ont_action_wan.time, "sleep", lambda *_: None)

    def persist(o, caps):  # SimpleNamespace ONT: no ORM state to flag
        o.tr069_last_snapshot = {"capabilities": caps}

    monkeypatch.setattr(ont_action_wan, "_persist_runtime_capabilities", persist)

    def fake_sav(client, device_id, params, **kwargs):
        if state.fail_on and any(state.fail_on in k for k in params):
            raise GenieACSError("readback mismatch")
        state.writes.append(dict(params))
        return {"_id": "spv"}

    monkeypatch.setattr(ont_action_wan, "set_and_verify", fake_sav)
    state.install = install
    state.ont = ont
    return state


def _run(requested=2):
    return ont_action_wan.set_pppoe_credentials(
        object(),
        "ont-1",
        username="u1",
        password="s3cret",
        instance_index=requested,
        wan_vlan=203,
    )


def test_wcd_absent_creates_parent_then_child_and_pins_index(harness) -> None:
    client = FakeClient({"1": dict(MGMT_WCD)})
    harness.install(client)
    result = _run()
    assert result.success is True
    assert client.added == [WCD, f"{WCD}2.WANPPPConnection."]
    base = f"{WCD}2.WANPPPConnection.1"
    first = harness.writes[0]
    assert set(first) == {
        f"{base}.{k}"
        for k in (
            "Username",
            "Password",
            "Enable",
            "ConnectionType",
            "X_HW_VLAN",
            "X_HW_SERVICELIST",
            "Name",
        )
    }
    assert harness.writes[1] == {f"{base}.NATEnabled": "true"}
    assert result.data["instance_index"] == 2


def test_only_management_wcd_is_never_touched(harness) -> None:
    client = FakeClient({"1": dict(MGMT_WCD)})
    harness.install(client)
    _run()
    touched = [p for w in harness.writes for p in w] + client.added
    assert not any("WANConnectionDevice.1." in p for p in touched)


def test_requested_management_wcd_fails_closed_without_writes(harness) -> None:
    client = FakeClient({"1": dict(MGMT_WCD)})
    harness.install(client)
    result = _run(requested=1)
    assert result.success is False
    assert "management" in result.message
    assert client.added == [] and harness.writes == []


def test_pending_parent_add_object_is_not_duplicated(harness) -> None:
    client = FakeClient({"1": dict(MGMT_WCD)}, parent_visible=False)
    harness.install(client)
    first = _run()
    assert first.success is False and first.waiting is True
    assert client.added == [WCD]
    second = _run()  # next inform, parent still not visible
    assert second.waiting is True
    assert client.added == [WCD]  # no second addObject
    # Parent appears: the blank WCD is adopted, child created once.
    client.wcds["2"] = dict(BLANK_WCD)
    third = _run()
    assert third.success is True
    assert client.added == [WCD, f"{WCD}2.WANPPPConnection."]


def test_reset_only_after_readback(harness) -> None:
    client = FakeClient({"1": dict(MGMT_WCD)})
    harness.install(client)
    harness.fail_on = "Username"
    failed = _run()
    assert failed.success is False
    assert not any(p.endswith(".Reset") for w in harness.writes for p in w)

    harness.fail_on = None
    ok = _run()
    assert ok.success is True
    flat = [p for w in harness.writes for p in w]
    reset = f"{WCD}2.WANPPPConnection.1.Reset"
    assert flat[-1] == reset
    assert flat.index(reset) > flat.index(f"{WCD}2.WANPPPConnection.1.NATEnabled")
    assert ok.data["reset_issued"] is True


def test_unchanged_username_does_not_reset(harness) -> None:
    wcd = dict(BLANK_WCD)
    wcd["WANPPPConnectionNumberOfEntries"] = {"_value": 1}
    wcd["WANPPPConnection"] = {
        "1": {"Username": {"_value": "u1"}, "X_HW_VLAN": {"_value": "203"}}
    }
    client = FakeClient({"1": dict(MGMT_WCD), "2": wcd})
    harness.install(client)
    result = _run()
    assert result.success is True and result.data["reset_issued"] is False
    assert client.added == []


def test_password_never_logged(harness, caplog) -> None:
    client = FakeClient({"1": dict(MGMT_WCD)})
    harness.install(client)
    with caplog.at_level("DEBUG"):
        _run()
    assert "s3cret" not in caplog.text


def test_unresolved_pppoe_wcd_fails_closed_in_provisioning() -> None:
    from app.services.network.ont_provision_steps import _resolve_igd_pppoe_wcd_index

    ont = SimpleNamespace(
        tr069_last_snapshot={
            "capabilities": {
                "wan": {
                    "data_model": "InternetGatewayDevice",
                    "connections": [{"index": 1, "detected_wan_service": "TR069"}],
                }
            }
        }
    )
    assert _resolve_igd_pppoe_wcd_index(ont, {}, 203) is None
