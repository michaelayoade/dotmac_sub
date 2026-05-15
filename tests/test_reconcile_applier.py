"""Tests for ``apply_plan``.

Stub OLT adapter and ACS client. The applier never does real I/O — its job
is to dispatch actions and translate adapter/client outcomes into
``ApplyResult`` records. Tests assert on the dispatch behavior + failure
mode translations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.network.reconcile import (
    AcsAddObject,
    AcsSetDhcpServer,
    AcsSetManagementServer,
    AcsSetNatEnabled,
    AcsSetPppoe,
    AcsSetWifiPassword,
    AcsSetWifiSsid,
    ApplyContext,
    ApplyResult,
    Drift,
    OltAuthorize,
    OltClearIphost,
    OltCreateServicePort,
    OltDeleteServicePort,
    OltIpconfig,
    OltModifyDescription,
    OltModifyLineProfile,
    OltModifyServiceProfile,
    OltOmciInternetConfig,
    OltOmciPppoe,
    OltOmciWanConfig,
    OltReset,
    OltTr069ServerConfig,
    Plan,
    ReconcileFailureReason,
    apply_plan,
)

# ── Stubs ───────────────────────────────────────────────────────────────────


class _StubOltAdapter:
    """Records every call. Returns success unless ``fail_on`` matches."""

    def __init__(self, *, fail_on: str | None = None, fail_message: str = "rejected"):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._fail_on = fail_on
        self._fail_message = fail_message

    def _result(self, method: str):
        success = method != self._fail_on
        return SimpleNamespace(
            success=success,
            message="ok" if success else self._fail_message,
        )

    def authorize_ont(self, *args, **kwargs):
        self.calls.append(("authorize_ont", args, kwargs))
        return self._result("authorize_ont")

    def update_ont_profiles(self, *args, **kwargs):
        self.calls.append(("update_ont_profiles", args, kwargs))
        return self._result("update_ont_profiles")

    def set_ont_description(self, *args, **kwargs):
        self.calls.append(("set_ont_description", args, kwargs))
        return self._result("set_ont_description")

    def clear_iphost_config(self, *args, **kwargs):
        self.calls.append(("clear_iphost_config", args, kwargs))
        return self._result("clear_iphost_config")

    def configure_iphost(self, *args, **kwargs):
        self.calls.append(("configure_iphost", args, kwargs))
        return self._result("configure_iphost")

    def bind_tr069_profile(self, *args, **kwargs):
        self.calls.append(("bind_tr069_profile", args, kwargs))
        return self._result("bind_tr069_profile")

    def create_service_port(self, *args, **kwargs):
        self.calls.append(("create_service_port", args, kwargs))
        return self._result("create_service_port")

    def delete_service_port(self, *args, **kwargs):
        self.calls.append(("delete_service_port", args, kwargs))
        return self._result("delete_service_port")

    def configure_pppoe(self, *args, **kwargs):
        self.calls.append(("configure_pppoe", args, kwargs))
        return self._result("configure_pppoe")

    def configure_internet_config(self, *args, **kwargs):
        self.calls.append(("configure_internet_config", args, kwargs))
        return self._result("configure_internet_config")

    def configure_wan_config(self, *args, **kwargs):
        self.calls.append(("configure_wan_config", args, kwargs))
        return self._result("configure_wan_config")

    def reboot_ont(self, *args, **kwargs):
        self.calls.append(("reboot_ont", args, kwargs))
        return self._result("reboot_ont")


class _StubAcsClient:
    """Records add_object + set_parameter_values calls. Returns task dict;
    can be configured to raise, surface a CR error, or surface a CWMP fault."""

    def __init__(
        self,
        *,
        raise_on: str | None = None,
        connection_request_error: str | None = None,
        cwmp_fault: dict | None = None,
    ):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._raise_on = raise_on
        self._cr_error = connection_request_error
        self._cwmp_fault = cwmp_fault

    def add_object(self, device_id, path):
        self.calls.append(("add_object", (device_id, path), {}))
        if self._raise_on == "add_object":
            raise RuntimeError("addObject blew up")
        return {"_id": "task-123"}

    def set_parameter_values(self, device_id, params, **kwargs):
        self.calls.append(("set_parameter_values", (device_id, params), kwargs))
        if self._raise_on == "set_parameter_values":
            raise RuntimeError("HTTP 500 from GenieACS")
        result: dict = {"_id": "task-abc"}
        if self._cr_error:
            result["connectionRequestError"] = self._cr_error
        if self._cwmp_fault:
            result["fault"] = self._cwmp_fault
        return result


def _ctx(**overrides) -> ApplyContext:
    return ApplyContext(
        olt_adapter=overrides.pop("olt_adapter", _StubOltAdapter()),
        acs_client=overrides.pop("acs_client", _StubAcsClient()),
        resolve_secret=overrides.pop("resolve_secret", lambda ref: f"PLAIN({ref})"),
    )


def _plan(*actions) -> Plan:
    return Plan(
        actions=actions,
        drifts=(
            Drift(
                field="x", surface="olt", desired=None, observed=None, repairable=True
            ),
        ),
        required_surfaces=frozenset({a.surface for a in actions}),
    )


# ── Empty plan ──────────────────────────────────────────────────────────────


def test_empty_plan_returns_success_with_no_actions():
    ctx = _ctx()
    result = apply_plan(Plan(actions=(), drifts=(), required_surfaces=frozenset()), ctx)
    assert isinstance(result, ApplyResult)
    assert result.success is True
    assert result.actions_applied == ()
    assert result.halted_by is None


# ── OLT action dispatch (happy path) ────────────────────────────────────────


def test_olt_authorize_dispatches_to_authorize_ont():
    olt = _StubOltAdapter()
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(
        OltAuthorize(
            fsp="0/1/3",
            ont_id=11,
            line_profile_id=40,
            service_profile_id=42,
            serial_number="HWTC8535819A",
            description="desc",
        )
    )
    result = apply_plan(plan, ctx)
    assert result.success is True
    method, args, kwargs = olt.calls[0]
    assert method == "authorize_ont"
    assert args == ("0/1/3", "HWTC8535819A")
    assert kwargs == {
        "line_profile_id": 40,
        "service_profile_id": 42,
        "description": "desc",
    }


def test_olt_modify_line_profile_dispatches_to_update_ont_profiles():
    olt = _StubOltAdapter()
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(OltModifyLineProfile(fsp="0/1/3", ont_id=11, line_profile_id=44))
    apply_plan(plan, ctx)
    assert olt.calls[0][0] == "update_ont_profiles"
    assert olt.calls[0][2] == {"line_profile_id": 44}


def test_olt_modify_service_profile_dispatches_to_update_ont_profiles():
    olt = _StubOltAdapter()
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(OltModifyServiceProfile(fsp="0/1/3", ont_id=11, service_profile_id=42))
    apply_plan(plan, ctx)
    assert olt.calls[0][2] == {"service_profile_id": 42}


def test_olt_clear_iphost_dispatches_with_ip_index():
    olt = _StubOltAdapter()
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(OltClearIphost(fsp="0/1/3", ont_id=11, ip_index=1))
    apply_plan(plan, ctx)
    assert olt.calls[0] == (
        "clear_iphost_config",
        ("0/1/3", 11),
        {"ip_index": 1},
    )


def test_olt_ipconfig_passes_full_static_config():
    olt = _StubOltAdapter()
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(
        OltIpconfig(
            fsp="0/1/3",
            ont_id=11,
            ip_index=0,
            ip_address="172.16.210.20",
            subnet_mask="255.255.255.0",
            gateway="172.16.210.1",
            vlan=201,
            priority=2,
            dns_primary="8.8.8.8",
            dns_secondary="4.2.2.2",
        )
    )
    apply_plan(plan, ctx)
    method, args, kwargs = olt.calls[0]
    assert method == "configure_iphost"
    assert kwargs["mode"] == "static"
    assert kwargs["ip_address"] == "172.16.210.20"
    assert kwargs["vlan"] == 201


def test_olt_tr069_dispatches_to_bind_tr069_profile():
    olt = _StubOltAdapter()
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(OltTr069ServerConfig(fsp="0/1/3", ont_id=11, profile_id=2))
    apply_plan(plan, ctx)
    assert olt.calls[0][0] == "bind_tr069_profile"
    assert olt.calls[0][2] == {"profile_id": 2}


def test_olt_create_service_port_passes_explicit_index():
    olt = _StubOltAdapter()
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(
        OltCreateServicePort(
            fsp="0/1/3",
            ont_id=11,
            service_port_index=23,
            vlan=201,
            gem_index=2,
            slot="mgmt",
        )
    )
    apply_plan(plan, ctx)
    method, args, kwargs = olt.calls[0]
    assert method == "create_service_port"
    assert kwargs["port_index"] == 23
    assert kwargs["gem_index"] == 2
    assert kwargs["vlan_id"] == 201


def test_olt_delete_service_port_passes_index():
    olt = _StubOltAdapter()
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(OltDeleteServicePort(service_port_index=99))
    apply_plan(plan, ctx)
    assert olt.calls[0] == ("delete_service_port", (99,), {})


def test_olt_omci_pppoe_resolves_password_ref():
    olt = _StubOltAdapter()
    ctx = _ctx(
        olt_adapter=olt,
        resolve_secret=lambda ref: "PLAIN_PW" if ref == "bao://pw" else ref,
    )
    plan = _plan(
        OltOmciPppoe(
            fsp="0/1/3",
            ont_id=11,
            ip_index=1,
            vlan=203,
            username="100024456",
            password_ref="bao://pw",
        )
    )
    apply_plan(plan, ctx)
    assert olt.calls[0][2]["password"] == "PLAIN_PW"
    assert olt.calls[0][2]["username"] == "100024456"


def test_olt_omci_internet_config_dispatches():
    olt = _StubOltAdapter()
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(OltOmciInternetConfig(fsp="0/1/3", ont_id=11, ip_index=1))
    apply_plan(plan, ctx)
    assert olt.calls[0][0] == "configure_internet_config"


def test_olt_omci_wan_config_dispatches():
    olt = _StubOltAdapter()
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(OltOmciWanConfig(fsp="0/1/3", ont_id=11, ip_index=1, profile_id=2))
    apply_plan(plan, ctx)
    assert olt.calls[0][0] == "configure_wan_config"
    assert olt.calls[0][2]["profile_id"] == 2


def test_olt_reset_dispatches_to_reboot_ont():
    olt = _StubOltAdapter()
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(OltReset(fsp="0/1/3", ont_id=11))
    apply_plan(plan, ctx)
    assert olt.calls[0] == ("reboot_ont", ("0/1/3", 11), {})


# ── OLT action dispatch (failure paths) ─────────────────────────────────────


def test_olt_failure_halts_with_olt_write_rejected():
    olt = _StubOltAdapter(fail_on="authorize_ont", fail_message="OLT busy")
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(
        OltAuthorize(
            fsp="0/1/3",
            ont_id=11,
            line_profile_id=40,
            service_profile_id=42,
            serial_number="HWTC8535819A",
            description="desc",
        ),
        OltReset(fsp="0/1/3", ont_id=11),  # should NOT execute
    )
    result = apply_plan(plan, ctx)
    assert result.success is False
    assert result.halted_by.reason == ReconcileFailureReason.OLT_WRITE_REJECTED
    assert "OLT busy" in result.halted_by.message
    assert result.actions_applied == ()  # authorize halted before completion
    # Subsequent reset never fired
    assert all(call[0] != "reboot_ont" for call in olt.calls)


def test_olt_modify_description_dispatches_to_set_ont_description():
    olt = _StubOltAdapter()
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(OltModifyDescription(fsp="0/1/3", ont_id=11, description="Kolawole_2"))
    result = apply_plan(plan, ctx)
    assert result.success is True
    method, args, _ = olt.calls[0]
    assert method == "set_ont_description"
    assert args == ("0/1/3", 11, "Kolawole_2")


# ── ACS action dispatch (happy path) ────────────────────────────────────────


def test_acs_add_object_calls_client():
    acs = _StubAcsClient()
    ctx = _ctx(acs_client=acs)
    plan = _plan(
        AcsAddObject(
            device_id="00259E-HG8546M-HWTC1",
            object_path="InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection",
        )
    )
    apply_plan(plan, ctx)
    assert acs.calls[0] == (
        "add_object",
        (
            "00259E-HG8546M-HWTC1",
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection",
        ),
        {},
    )


def test_acs_pppoe_pushes_six_params_including_resolved_password():
    acs = _StubAcsClient()
    ctx = _ctx(
        acs_client=acs,
        resolve_secret=lambda ref: "PW_RESOLVED" if ref == "bao://pw" else ref,
    )
    plan = _plan(
        AcsSetPppoe(
            device_id="00259E-HG8546M-HWTC1",
            wcd_index=1,
            instance_index=1,
            username="100024456",
            password_ref="bao://pw",
            vlan=203,
        )
    )
    apply_plan(plan, ctx)
    method, args, _ = acs.calls[0]
    assert method == "set_parameter_values"
    params = args[1]
    assert len(params) == 6
    pppoe_root = (
        "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1."
    )
    assert params[pppoe_root + "Username"] == "100024456"
    assert params[pppoe_root + "Password"] == "PW_RESOLVED"
    assert params[pppoe_root + "X_HW_VLAN"] == 203
    assert params[pppoe_root + "Enable"] is True
    assert params[pppoe_root + "ConnectionType"] == "IP_Routed"


def test_acs_set_wifi_ssid_pushes_single_param():
    acs = _StubAcsClient()
    ctx = _ctx(acs_client=acs)
    plan = _plan(AcsSetWifiSsid(device_id="dev", ssid="KURSI"))
    apply_plan(plan, ctx)
    params = acs.calls[0][1][1]
    assert params == {
        "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID": "KURSI"
    }


def test_acs_set_wifi_password_pushes_resolved_psk_and_records_redacted():
    acs = _StubAcsClient()
    ctx = _ctx(
        acs_client=acs,
        resolve_secret=lambda ref: "ACTUAL_PSK" if ref == "bao://wifi" else ref,
    )
    plan = _plan(AcsSetWifiPassword(device_id="dev", password_ref="bao://wifi"))
    result = apply_plan(plan, ctx)

    # The pushed value is the resolved plaintext...
    params = acs.calls[0][1][1]
    assert (
        params[
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1."
            "PreSharedKey.1.KeyPassphrase"
        ]
        == "ACTUAL_PSK"
    )
    # ...but the AppliedAction record carries "[redacted]" so logs/audit
    # never surface the password.
    assert result.actions_applied[0].new_value == "[redacted]"


def test_acs_set_nat_enabled_pushes_single_param():
    acs = _StubAcsClient()
    ctx = _ctx(acs_client=acs)
    plan = _plan(
        AcsSetNatEnabled(
            device_id="dev",
            wcd_index=1,
            instance_index=1,
            enabled=True,
        )
    )
    apply_plan(plan, ctx)
    params = acs.calls[0][1][1]
    assert params == {
        "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1."
        "WANPPPConnection.1.NATEnabled": True
    }


def test_acs_set_dhcp_server_pushes_four_params():
    acs = _StubAcsClient()
    ctx = _ctx(acs_client=acs)
    plan = _plan(
        AcsSetDhcpServer(
            device_id="dev",
            enabled=True,
            pool_min="192.168.100.2",
            pool_max="192.168.100.254",
            subnet_mask="255.255.255.0",
        )
    )
    apply_plan(plan, ctx)
    params = acs.calls[0][1][1]
    assert len(params) == 4
    lan_root = "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement."
    assert params[lan_root + "DHCPServerEnable"] is True
    assert params[lan_root + "MinAddress"] == "192.168.100.2"
    assert params[lan_root + "SubnetMask"] == "255.255.255.0"


def test_acs_set_management_server_pushes_cr_creds_and_inform_interval():
    acs = _StubAcsClient()
    ctx = _ctx(
        acs_client=acs,
        resolve_secret=lambda ref: "PW" if ref == "bao://cr" else ref,
    )
    plan = _plan(
        AcsSetManagementServer(
            device_id="dev",
            cr_username="admin",
            cr_password_ref="bao://cr",
            inform_interval_sec=300,
        )
    )
    apply_plan(plan, ctx)
    params = acs.calls[0][1][1]
    ms_root = "InternetGatewayDevice.ManagementServer."
    assert params[ms_root + "ConnectionRequestUsername"] == "admin"
    assert params[ms_root + "ConnectionRequestPassword"] == "PW"
    assert params[ms_root + "PeriodicInformInterval"] == 300


# ── Secret resolver fail-paths ──────────────────────────────────────────────


def test_resolver_exception_during_acs_psk_push_maps_to_acs_write_faulted():
    """If the OpenBao resolver explodes (5xx, network timeout) on a
    WiFi-password push, the applier halts with ACS_WRITE_FAULTED so the
    operator sees the failing action + a clear message rather than an
    unhandled 500."""
    acs = _StubAcsClient()

    def _exploding_resolver(ref):
        raise RuntimeError("OpenBao 503")

    ctx = _ctx(acs_client=acs, resolve_secret=_exploding_resolver)
    plan = _plan(AcsSetWifiPassword(device_id="dev", password_ref="bao://wifi"))
    result = apply_plan(plan, ctx)

    assert result.success is False
    assert result.halted_by.reason == "acs_write_faulted"
    assert "secret resolution failed" in result.halted_by.message.lower()
    assert "OpenBao 503" in result.halted_by.message
    # The ACS NBI client was never called — the failure preceded the push.
    assert acs.calls == []


def test_resolver_none_return_during_pppoe_push_maps_to_acs_write_faulted():
    """resolve_secret returning None indicates the secret slot exists but
    is empty — distinct failure mode from resolver crashes, still maps
    to ACS_WRITE_FAULTED."""
    acs = _StubAcsClient()
    ctx = _ctx(acs_client=acs, resolve_secret=lambda ref: None)
    plan = _plan(
        AcsSetPppoe(
            device_id="dev",
            wcd_index=1,
            instance_index=1,
            username="100024456",
            password_ref="bao://pppoe",
            vlan=203,
        )
    )
    result = apply_plan(plan, ctx)

    assert result.success is False
    assert result.halted_by.reason == "acs_write_faulted"
    assert "none" in result.halted_by.message.lower()
    assert acs.calls == []


def test_resolver_failure_during_cr_password_push_halts_acs_management_server():
    """ACS management-server push also pulls a CR password from the
    resolver. Same fail-path."""
    acs = _StubAcsClient()
    ctx = _ctx(
        acs_client=acs,
        resolve_secret=lambda ref: (_ for _ in ()).throw(
            RuntimeError("KV path missing")
        ),
    )
    plan = _plan(
        AcsSetManagementServer(
            device_id="dev",
            cr_username="admin",
            cr_password_ref="bao://cr",
            inform_interval_sec=300,
        )
    )
    result = apply_plan(plan, ctx)

    assert result.success is False
    assert result.halted_by.reason == "acs_write_faulted"
    assert "KV path missing" in result.halted_by.message


def test_resolver_empty_ref_does_not_call_resolver():
    """An empty password_ref shouldn't crash the resolver — it just
    resolves to empty plaintext and the action proceeds."""
    acs = _StubAcsClient()
    calls: list[str] = []

    def _tracking_resolver(ref):
        calls.append(ref)
        return ref

    ctx = _ctx(acs_client=acs, resolve_secret=_tracking_resolver)
    plan = _plan(AcsSetWifiPassword(device_id="dev", password_ref=""))
    result = apply_plan(plan, ctx)
    assert result.success is True
    # _resolve_or_fail short-circuits empty refs without invoking the
    # resolver at all.
    assert calls == []


# ── ACS action dispatch (failure paths) ─────────────────────────────────────


def test_acs_connection_request_error_maps_to_acs_cr_failed():
    """Today's Fix #5 surfaces empty-CR-credential failures via
    ``connectionRequestError`` on the task dict. The applier translates that
    into ``ACS_CR_FAILED`` so the operator knows to force an OLT reset."""
    acs = _StubAcsClient(connection_request_error="empty CR username")
    ctx = _ctx(acs_client=acs)
    plan = _plan(
        AcsSetWifiSsid(device_id="dev", ssid="KURSI"),
        AcsSetDhcpServer(
            device_id="dev",
            enabled=True,
            pool_min="192.168.100.2",
            pool_max="192.168.100.254",
            subnet_mask="255.255.255.0",
        ),  # should not run
    )
    result = apply_plan(plan, ctx)
    assert result.success is False
    assert result.halted_by.reason == ReconcileFailureReason.ACS_CR_FAILED
    assert "empty CR username" in result.halted_by.message
    # Second action never fired
    assert len(acs.calls) == 1


def test_acs_cwmp_fault_maps_to_acs_write_faulted():
    acs = _StubAcsClient(cwmp_fault={"code": "9002", "message": "Internal error"})
    ctx = _ctx(acs_client=acs)
    plan = _plan(AcsSetWifiSsid(device_id="dev", ssid="KURSI"))
    result = apply_plan(plan, ctx)
    assert result.success is False
    assert result.halted_by.reason == ReconcileFailureReason.ACS_WRITE_FAULTED
    assert "9002" in result.halted_by.message


def test_acs_genieacs_exception_maps_to_acs_write_faulted():
    acs = _StubAcsClient(raise_on="set_parameter_values")
    ctx = _ctx(acs_client=acs)
    plan = _plan(AcsSetWifiSsid(device_id="dev", ssid="KURSI"))
    result = apply_plan(plan, ctx)
    assert result.success is False
    assert result.halted_by.reason == ReconcileFailureReason.ACS_WRITE_FAULTED


def test_acs_add_object_exception_maps_to_acs_write_faulted():
    acs = _StubAcsClient(raise_on="add_object")
    ctx = _ctx(acs_client=acs)
    plan = _plan(AcsAddObject(device_id="dev", object_path="X.Y.Z"))
    result = apply_plan(plan, ctx)
    assert result.success is False
    assert result.halted_by.reason == ReconcileFailureReason.ACS_WRITE_FAULTED


# ── Timeout ─────────────────────────────────────────────────────────────────


def test_deadline_exceeded_halts_with_timeout():
    ctx = _ctx()
    plan = _plan(OltReset(fsp="0/1/3", ont_id=11))
    past = datetime.now(UTC) - timedelta(seconds=1)
    result = apply_plan(plan, ctx, deadline=past)
    assert result.success is False
    assert result.halted_by.reason == ReconcileFailureReason.TIMEOUT
    assert result.actions_applied == ()


def test_no_deadline_means_no_apply_side_cap():
    olt = _StubOltAdapter()
    ctx = _ctx(olt_adapter=olt)
    plan = _plan(OltReset(fsp="0/1/3", ont_id=11))
    result = apply_plan(plan, ctx)  # deadline=None
    assert result.success is True


# ── Applied actions list ───────────────────────────────────────────────────


def test_applied_actions_reflect_full_successful_sequence():
    olt = _StubOltAdapter()
    acs = _StubAcsClient()
    ctx = _ctx(olt_adapter=olt, acs_client=acs)
    plan = _plan(
        OltClearIphost(fsp="0/1/3", ont_id=11, ip_index=0),
        OltIpconfig(
            fsp="0/1/3",
            ont_id=11,
            ip_index=0,
            ip_address="172.16.210.20",
            subnet_mask="255.255.255.0",
            gateway="172.16.210.1",
            vlan=201,
            priority=2,
            dns_primary="8.8.8.8",
            dns_secondary="4.2.2.2",
        ),
        AcsSetWifiSsid(device_id="dev", ssid="KURSI"),
    )
    result = apply_plan(plan, ctx)
    assert result.success is True
    assert len(result.actions_applied) == 3
    surfaces = [a.surface for a in result.actions_applied]
    assert surfaces == ["olt", "olt", "acs"]


def test_applied_actions_record_duration():
    ctx = _ctx()
    plan = _plan(OltReset(fsp="0/1/3", ont_id=11))
    result = apply_plan(plan, ctx)
    assert result.actions_applied[0].duration_ms >= 0


# ── Passthrough secret resolver (default) ───────────────────────────────────


def test_default_secret_resolver_passes_ref_through_as_plaintext():
    """When the operator hasn't wired OpenBao yet, the default identity
    resolver lets callers pass plaintext directly via the ref field."""
    from app.services.network.reconcile import passthrough_secret

    assert passthrough_secret("hello") == "hello"
    assert passthrough_secret("bao://x/y") == "bao://x/y"


def test_apply_uses_passthrough_when_no_resolver_provided():
    olt = _StubOltAdapter()
    ctx = ApplyContext(olt_adapter=olt, acs_client=_StubAcsClient())
    plan = _plan(
        OltOmciPppoe(
            fsp="0/1/3",
            ont_id=11,
            ip_index=1,
            vlan=203,
            username="u",
            password_ref="literal-pw",
        )
    )
    apply_plan(plan, ctx)
    assert olt.calls[0][2]["password"] == "literal-pw"


# ── Post-addObject WAN PPP instance discovery ───────────────────────────────


class _RefreshAwareAcsClient(_StubAcsClient):
    """Extends _StubAcsClient with refresh_object + list_devices, so the
    applier's post-addObject discovery probe can run. ``post_refresh_doc``
    is what list_devices returns after a refresh — the test arranges this
    to simulate the device's reported instance index."""

    def __init__(self, *, post_refresh_doc=None, **kwargs):
        super().__init__(**kwargs)
        self._post_refresh_doc = post_refresh_doc

    def refresh_object(self, device_id, object_path, *, allow_when_pending=False):
        self.calls.append(
            ("refresh_object", (device_id, object_path), {"allow_when_pending": allow_when_pending})
        )
        return {"_id": "refresh-task"}

    def list_devices(self, query=None, projection=None):
        self.calls.append(("list_devices", (query, projection), {}))
        return [self._post_refresh_doc] if self._post_refresh_doc else []


def _wan_ppp_doc(*, wcd: int, instance_keys: list[int]) -> dict:
    """Build a minimal GenieACS device document with the named
    WANPPPConnection child instances under the given WCD slot."""
    children = {str(k): {"_object": True} for k in instance_keys}
    return {
        "_id": "00259E-HG8546M-HWTC7C7E1D92",
        "InternetGatewayDevice": {
            "WANDevice": {
                "1": {
                    "WANConnectionDevice": {
                        str(wcd): {"WANPPPConnection": children}
                    }
                }
            }
        },
    }


def test_acs_add_object_records_device_returned_wan_ppp_instance_for_downstream_writes():
    """The planner predicts WANPPPConnection.1 by default, but the device's
    monotonic instance counter may have advanced past .1 (when prior cycles
    created and deleted instances). After addObject, the applier refreshes
    the parent and reads back the highest child key, then uses that index
    for downstream AcsSetPppoe / AcsSetNatEnabled. Without this, the
    Username/Password push lands on a non-existent .1 path and silently
    no-ops on HG8546M V5R019C10S100 — exactly the Matrix Global Apartment
    bug class."""
    acs = _RefreshAwareAcsClient(
        post_refresh_doc=_wan_ppp_doc(wcd=2, instance_keys=[3]),
    )
    ctx = _ctx(acs_client=acs)
    plan = _plan(
        AcsAddObject(
            device_id="00259E-HG8546M-HWTC7C7E1D92",
            object_path="InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2.WANPPPConnection",
        ),
        AcsSetPppoe(
            device_id="00259E-HG8546M-HWTC7C7E1D92",
            wcd_index=2,
            instance_index=1,  # planner's default guess
            username="100025915",
            password_ref="g2qMjOz7",
            vlan=203,
        ),
        AcsSetNatEnabled(
            device_id="00259E-HG8546M-HWTC7C7E1D92",
            wcd_index=2,
            instance_index=1,  # planner's default guess
            enabled=True,
        ),
    )
    result = apply_plan(plan, ctx)
    assert result.success is True
    # Override stashed on the context — and persists for the rest of this apply pass.
    assert ctx.wan_ppp_instances == {2: 3}
    # AcsSetPppoe used .3, not the planner's .1.
    pppoe_calls = [c for c in acs.calls if c[0] == "set_parameter_values"]
    pppoe_params = pppoe_calls[0][1][1]
    pppoe_keys = list(pppoe_params.keys())
    assert all("WANPPPConnection.3." in k for k in pppoe_keys)
    assert not any("WANPPPConnection.1." in k for k in pppoe_keys)
    # AcsSetNatEnabled also rewritten to .3.
    nat_params = pppoe_calls[1][1][1]
    nat_keys = list(nat_params.keys())
    assert all("WANPPPConnection.3." in k for k in nat_keys)


def test_acs_add_object_falls_back_to_planned_index_when_discovery_unavailable():
    """The stub client has no refresh_object / list_devices. The applier
    must still run the addObject and the downstream writes — discovery is
    best-effort, never a hard requirement. Falls back to the planner's
    predicted instance_index (which is correct in the common case where
    the device's counter is at .1)."""
    acs = _StubAcsClient()  # no refresh_object / list_devices
    ctx = _ctx(acs_client=acs)
    plan = _plan(
        AcsAddObject(
            device_id="dev",
            object_path="InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection",
        ),
        AcsSetPppoe(
            device_id="dev",
            wcd_index=1,
            instance_index=1,
            username="u",
            password_ref="p",
            vlan=203,
        ),
    )
    result = apply_plan(plan, ctx)
    assert result.success is True
    assert ctx.wan_ppp_instances == {}
    pppoe_params = [c for c in acs.calls if c[0] == "set_parameter_values"][0][1][1]
    pppoe_keys = list(pppoe_params.keys())
    assert all("WANPPPConnection.1." in k for k in pppoe_keys)


def test_acs_add_object_swallows_refresh_failures_silently():
    """The post-condition probe is best-effort. A refresh_object that
    raises (CR timeout, NBI hiccup) must not break the apply pass — the
    addObject has already succeeded. Downstream writes fall back to the
    planner's predicted instance_index."""

    class _FlakyRefreshAcsClient(_StubAcsClient):
        def refresh_object(self, device_id, object_path, *, allow_when_pending=False):
            raise RuntimeError("CR timed out")

        def list_devices(self, query=None, projection=None):
            return []

    acs = _FlakyRefreshAcsClient()
    ctx = _ctx(acs_client=acs)
    plan = _plan(
        AcsAddObject(
            device_id="dev",
            object_path="InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2.WANPPPConnection",
        ),
        AcsSetPppoe(
            device_id="dev",
            wcd_index=2,
            instance_index=1,
            username="u",
            password_ref="p",
            vlan=203,
        ),
    )
    result = apply_plan(plan, ctx)
    assert result.success is True
    assert ctx.wan_ppp_instances == {}


def test_acs_add_object_skips_discovery_for_non_wan_ppp_targets():
    """addObject is also used for other object kinds (e.g. WANIPConnection,
    PortMapping). The post-condition probe only applies when the target
    parent path is …WANConnectionDevice.<N>.WANPPPConnection, so unrelated
    addObject calls don't pay for an extra refresh + lookup."""
    acs = _RefreshAwareAcsClient(post_refresh_doc=_wan_ppp_doc(wcd=2, instance_keys=[5]))
    ctx = _ctx(acs_client=acs)
    plan = _plan(
        AcsAddObject(
            device_id="dev",
            object_path="InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection",
        ),
    )
    result = apply_plan(plan, ctx)
    assert result.success is True
    # No refresh_object / list_devices probe happened.
    assert not any(c[0] == "refresh_object" for c in acs.calls)
    assert not any(c[0] == "list_devices" for c in acs.calls)
    assert ctx.wan_ppp_instances == {}
