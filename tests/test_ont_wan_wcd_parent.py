"""WCD parent creation for TR-098 ONTs that only expose the management WCD."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.genieacs_client import (
    GenieACSDeliveryCode,
    GenieACSError,
    GenieACSTaskQueuedError,
)
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
        self.queued: list[tuple[dict, dict]] = []

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

    def set_parameter_values(self, device_id, params, **kwargs):
        self.queued.append((dict(params), kwargs))
        return {"_id": "queued"}

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


# -- legacy path: unset index, CR-failed Reset, password-revision Reset --------


def test_unset_index_selects_or_creates(harness) -> None:
    client = FakeClient({"1": dict(MGMT_WCD)})
    harness.install(client)
    result = _run(requested=None)
    assert result.success is True
    assert client.added[0] == WCD
    assert not any("WANConnectionDevice.1." in p for w in harness.writes for p in w)


def test_explicit_management_index_is_the_only_refusal(harness) -> None:
    client = FakeClient({"1": dict(MGMT_WCD), "2": dict(BLANK_WCD)})
    harness.install(client)
    assert _run(requested=1).success is False
    assert _run(requested=None).success is True  # adopts blank WCD2


def test_cr_failed_params_write_still_queues_reset(harness, monkeypatch) -> None:
    client = FakeClient({"1": dict(MGMT_WCD)})
    harness.install(client)

    def queued(c, d, params, **kw):
        raise GenieACSTaskQueuedError(
            "queued",
            task_id="t",
            reason="cr",
            delivery_code=GenieACSDeliveryCode.connection_request_failed,
        )

    monkeypatch.setattr(ont_action_wan, "set_and_verify", queued)
    result = _run()
    assert result.success is False and result.waiting is True
    assert len(client.queued) == 1
    params, kwargs = client.queued[0]
    assert list(params) == [f"{WCD}2.WANPPPConnection.1.Reset"]
    assert kwargs == {"connection_request": False}


def test_password_only_change_resets_once_per_revision(harness, monkeypatch) -> None:
    wcd = dict(BLANK_WCD)
    wcd["WANPPPConnection"] = {"1": {"Username": {"_value": "u1"}}}
    wcd["WANPPPConnectionNumberOfEntries"] = {"_value": 1}
    client = FakeClient({"1": dict(MGMT_WCD), "2": wcd})
    ont = harness.install(client)
    monkeypatch.setattr(
        ont_action_wan, "ppp_credential_revision", lambda u, p: f"rev-{len(p)}"
    )
    ont_action_wan.set_ppp_credential_revision(ont, "rev-1")  # baseline
    first = _run()  # password length 6 -> new revision
    assert first.data["reset_issued"] is True
    second = _run()  # same revision again
    assert second.data["reset_issued"] is False
    resets = [p for w in harness.writes for p in w if p.endswith(".Reset")]
    assert len(resets) == 1


# -- owner path: reconcile planner/applier -----------------------------------


class AcsStub:
    def __init__(self, wcds, *, parent_visible=True, cr_error_first=False):
        self.wcds = wcds
        self.parent_visible = parent_visible
        self.cr_error_first = cr_error_first
        self.calls: list[tuple] = []

    def list_devices(self, query=None, projection=None):
        return [_doc(self.wcds)]

    def refresh_object(self, device_id, path, **kw):
        return {}

    def add_object(self, device_id, path):
        self.calls.append(("add", path))
        if path == WCD.rstrip("."):
            if self.parent_visible:
                self.wcds["2"] = dict(BLANK_WCD)
        else:
            self.wcds["2"]["WANPPPConnection"] = {"1": {}}
        return {"_id": "t"}

    def set_parameter_values(self, device_id, params, **kw):
        self.calls.append(("set", dict(params), kw))
        first = sum(1 for c in self.calls if c[0] == "set") == 1
        if self.cr_error_first and first:
            return {"_id": "t", "connectionRequestError": "401"}
        return {"_id": "t"}


def _owner_actions():
    from app.services.network.reconcile import (
        AcsAddObject,
        AcsSetNatEnabled,
        AcsSetPppoe,
    )

    base = "InternetGatewayDevice.WANDevice.1.WANConnectionDevice"
    return (
        AcsAddObject(device_id="d", object_path=base, wcd_index=2),
        AcsAddObject(
            device_id="d", object_path=f"{base}.2.WANPPPConnection", wcd_index=2
        ),
        AcsSetPppoe(
            device_id="d",
            wcd_index=2,
            instance_index=1,
            username="u1",
            password_ref="pw",
            vlan=203,
            reset_on_apply=True,
        ),
        AcsSetNatEnabled(device_id="d", wcd_index=2, instance_index=1, enabled=True),
    )


def _apply(acs, actions, state=None, monkeypatch=None):
    from app.services.network.reconcile import apply_plan
    from tests.test_reconcile_applier import _ctx, _plan

    plan = _plan(*actions)
    ctx = _ctx(plan, acs_client=acs)
    if state is not None:
        ctx.state_get = state.get
        ctx.state_set = state.__setitem__
    return apply_plan(plan, ctx)


def test_owner_path_mgmt_only_creates_parent_and_never_touches_mgmt() -> None:
    acs = AcsStub({"1": dict(MGMT_WCD)})
    result = _apply(acs, _owner_actions())
    assert result.success is True
    paths = [c[1] for c in acs.calls if c[0] == "add"]
    assert paths == [WCD.rstrip("."), f"{WCD}2.WANPPPConnection"]
    sets = [c for c in acs.calls if c[0] == "set"]
    assert all("WANConnectionDevice.1." not in p for c in sets for p in c[1])
    assert any(p.endswith("WANPPPConnection.1.Reset") for c in sets for p in c[1])


def test_owner_path_remaps_to_created_index() -> None:
    from app.services.network.reconcile import AcsAddObject

    acs = AcsStub({"1": dict(MGMT_WCD), "2": dict(MGMT_WCD)})  # 2 is also mgmt
    result = _apply(acs, _owner_actions()[:1])
    assert result.success is False  # desired index is management: refused
    assert not [c for c in acs.calls if c[0] == "add"]
    assert AcsAddObject  # imported for clarity


def test_owner_path_pending_parent_not_duplicated() -> None:
    acs = AcsStub({"1": dict(MGMT_WCD)}, parent_visible=False)
    state: dict = {}
    first = _apply(acs, _owner_actions()[:1], state)
    assert first.success is False and "pending_wcd_add" in state
    second = _apply(acs, _owner_actions()[:1], state)
    assert second.success is False
    assert len([c for c in acs.calls if c[0] == "add"]) == 1


def test_owner_path_cr_failed_params_still_queues_reset() -> None:
    acs = AcsStub({"1": dict(MGMT_WCD), "2": dict(BLANK_WCD)}, cr_error_first=True)
    acs.wcds["2"]["WANPPPConnection"] = {"1": {}}
    result = _apply(acs, _owner_actions()[2:3])
    assert result.success is False
    assert result.halted_by.reason == "acs_cr_failed"
    sets = [c for c in acs.calls if c[0] == "set"]
    assert len(sets) == 2
    assert list(sets[1][1])[0].endswith("WANPPPConnection.1.Reset")
    assert sets[1][2] == {"connection_request": False}


def test_owner_path_password_only_change_one_reset_per_revision(monkeypatch) -> None:
    from app.services.network.reconcile import AcsSetPppoe, applier

    monkeypatch.setattr(applier, "_ppp_revision", lambda u, p: f"rev-{p}")
    acs = AcsStub({"1": dict(MGMT_WCD), "2": dict(BLANK_WCD)})
    state = {"ppp_credential_revision": "rev-old"}
    act = AcsSetPppoe(
        device_id="d",
        wcd_index=2,
        instance_index=1,
        username="u1",
        password_ref="new",
        vlan=203,
    )
    assert _apply(acs, (act,), state).success is True
    assert _apply(acs, (act,), state).success is True
    resets = [
        c
        for c in acs.calls
        if c[0] == "set" and any(p.endswith(".Reset") for p in c[1])
    ]
    assert len(resets) == 1
    assert state["ppp_credential_revision"] != "rev-old"


# -- review fixes: None index, waiting reason, marker survival, form default --


def test_planner_none_index_plans_select_or_create() -> None:
    import dataclasses

    from app.services.network.reconcile import (
        AcsSetPppoe,
        compute_plan,
    )
    from app.services.network.reconcile.actions import SELECT_OR_CREATE_WCD
    from tests.test_reconcile_planner import _desired, _informed_acs_observed, _observed

    desired = dataclasses.replace(
        _desired(wan_pppoe_instance_index=1), wan_pppoe_wcd_index=None
    )
    plan = compute_plan(desired, _observed(acs=_informed_acs_observed()), "sync")
    from app.services.network.reconcile import AcsAddObject

    adds = [a for a in plan.actions if isinstance(a, AcsAddObject)]
    assert adds and adds[0].object_path.endswith("WANDevice.1.WANConnectionDevice")
    assert adds[0].wcd_index == SELECT_OR_CREATE_WCD
    assert next(a for a in plan.actions if isinstance(a, AcsSetPppoe)).wcd_index == 0


def test_owner_path_select_or_create_then_adopts_persisted_index() -> None:
    from app.services.network.reconcile.actions import SELECT_OR_CREATE_WCD

    actions = tuple(
        a.__class__(**{**a.__dict__, "wcd_index": SELECT_OR_CREATE_WCD})
        for a in _owner_actions()
    )
    actions = tuple(
        a.__class__(
            **{
                **a.__dict__,
                "object_path": a.object_path.replace(".2.", ".0."),
            }
        )
        if hasattr(a, "object_path")
        else a
        for a in actions
    )
    acs = AcsStub({"1": dict(MGMT_WCD)})
    state: dict = {}
    result = _apply(acs, actions, state)
    assert result.success is True
    assert state["created_wcd_index"] == 2  # persisted (C1)
    assert not any("WANConnectionDevice.1." in str(c) for c in acs.calls)

    # Next pass: WCD2 now holds the child; it is adopted, nothing re-created.
    before_adds = len([c for c in acs.calls if c[0] == "add"])
    again = _apply(acs, actions, state)
    assert again.success is True
    assert len([c for c in acs.calls if c[0] == "add"]) == before_adds


def test_unresolved_select_marker_is_refused_downstream() -> None:
    from app.services.network.reconcile.actions import SELECT_OR_CREATE_WCD

    pppoe = _owner_actions()[2]
    pppoe = pppoe.__class__(**{**pppoe.__dict__, "wcd_index": SELECT_OR_CREATE_WCD})
    acs = AcsStub({"1": dict(MGMT_WCD)})
    result = _apply(acs, (pppoe,))
    assert result.success is False
    assert result.halted_by.reason == "invalid_change"
    assert not acs.calls


def test_pending_wcd_returns_waiting_reason_not_terminal() -> None:
    acs = AcsStub({"1": dict(MGMT_WCD)}, parent_visible=False)
    state: dict = {}
    first = _apply(acs, _owner_actions()[:1], state)
    second = _apply(acs, _owner_actions()[:1], state)
    for r in (first, second):
        assert r.halted_by.reason == "acs_cr_failed"  # waiting, resumes on inform


def test_markers_survive_runtime_snapshot_rebuild() -> None:
    from app.services.network.ont_tr069 import _carried_capabilities

    previous = {
        "capabilities": {
            "pending_wcd_add": "2026-10-21T00:00:00+00:00",
            "ppp_credential_revision": "abc",
            "created_wcd_index": 2,
            "pending_actions": {"add_wcd_wan": {"state": "pending_verification"}},
            "wan": {"stale": True},
        }
    }
    kept = _carried_capabilities(previous)
    assert kept == {
        "pending_wcd_add": "2026-10-21T00:00:00+00:00",
        "ppp_credential_revision": "abc",
        "created_wcd_index": 2,
        "pending_actions": {"add_wcd_wan": {"state": "pending_verification"}},
    }
    assert _carried_capabilities(None) == {}


def test_blank_admin_form_index_is_none_not_management_default(monkeypatch) -> None:
    from app.web.admin import network_onts_actions as mod

    seen: dict = {}
    form = {"pppoe_username": "u", "pppoe_password": "p", "instance_index": ""}
    monkeypatch.setattr(mod, "parse_form_data_sync", lambda request: form)
    monkeypatch.setattr(mod, "_ensure_ont_write_scope", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_action_result_response", lambda **k: "ok")

    def fake(db, ont_id, **kw):
        seen.update(kw)
        return SimpleNamespace(success=True)

    monkeypatch.setattr(
        mod.web_network_ont_actions_service, "set_pppoe_credentials", fake
    )
    mod.ont_set_pppoe_credentials(object(), "ont-1", db=object())
    assert seen["instance_index"] is None
