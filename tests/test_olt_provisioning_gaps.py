"""Tests for OLT/VLAN/TR-069 command and manual-action coverage.

Covers:
- Service-port SSH command parsing and filtering (Phase 1)
- VLAN chain validation (Phase 1)
- Huawei command generation from provisioning profiles (Phase 3)
- Web service wrappers (Phase 2)
- Route registration (all phases)
- OLT profile SSH output parsing (Phase 3)
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from starlette.routing import Route

from app.models.network import (
    OltCard,
    OltCardPort,
    OltShelf,
    OLTDevice,
    OntAssignment,
    OntProvisioningProfile,
    OntUnit,
    PonPort,
    PppoePasswordMode,
    Splitter,
    SplitterPort,
    VlanMode,
)
from app.models.ont_autofind import OltAutofindCandidate
from app.models.subscriber import Subscriber, SubscriberCategory
from app.services.network.olt_command_gen import (
    HuaweiCommandGenerator,
    OltCommandSet,
    OntProvisioningContext,
    ProvisioningSpec,
    WanServiceSpec,
    build_spec_from_profile,
)
from app.services.network.olt_ssh import (
    OltProfileEntry,
    ServicePortEntry,
    _parse_profile_table,
    _parse_service_port_table,
)
from app.services.network.vlan_chain import (
    VlanChainResult,
    VlanChainWarning,
    validate_chain,
)
from app.services.network import olt_authorization_workflow


def _create_business_subscriber(db_session) -> Subscriber:
    """Create a minimal business subscriber for ownership constraints."""
    subscriber = Subscriber(
        first_name="Test",
        last_name="Business",
        email=f"business-{uuid.uuid4().hex[:8]}@example.test",
        company_name=f"Test Org {uuid.uuid4().hex[:8]}",
    )
    subscriber.category = SubscriberCategory.business
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


def test_reference_ont_options_include_huawei_dotted_external_ids(db_session) -> None:
    from app.services.web_network_service_ports import _reference_ont_options

    olt = OLTDevice(name="Reference OLT", vendor="Huawei", model="MA5608T")
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)

    pon = PonPort(olt_id=olt.id, name="0/2/1")
    db_session.add(pon)
    db_session.commit()
    db_session.refresh(pon)

    target = OntUnit(serial_number="TARGET-ONT", board="0/2", port="1", external_id="5")
    ref = OntUnit(
        serial_number="REF-ONT",
        board="0/2",
        port="1",
        external_id="huawei:4194320640.7",
    )
    db_session.add_all([target, ref])
    db_session.commit()
    db_session.refresh(target)
    db_session.refresh(ref)

    db_session.add_all(
        [
            OntAssignment(ont_unit_id=target.id, pon_port_id=pon.id, active=True),
            OntAssignment(ont_unit_id=ref.id, pon_port_id=pon.id, active=True),
        ]
    )
    db_session.commit()

    options = _reference_ont_options(
        db_session,
        target_ont_id=str(target.id),
        olt_id=str(olt.id),
    )

    assert len(options) == 1
    assert options[0]["id"] == str(ref.id)
    assert "ONT-ID 7" in options[0]["label"]


def test_authorize_autofind_logs_disappeared_candidate_after_refresh(
    db_session, monkeypatch, caplog
) -> None:
    olt = OLTDevice(name="SPDC Huawei OLT", vendor="Huawei", model="MA5608T")
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)

    monkeypatch.setattr(
        "app.services.web_network_olts.get_olt_or_none",
        lambda *_args, **_kwargs: olt,
    )
    monkeypatch.setattr(
        olt_authorization_workflow,
        "get_autofind_candidate_by_serial",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.web_network_ont_autofind.sync_olt_autofind_candidates",
        lambda *_args, **_kwargs: (True, "Refreshed autofind cache.", {"resolved": 1}),
    )

    caplog.set_level("WARNING")

    result = olt_authorization_workflow.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/3",
        "UBNT-F9AA7344",
    )

    assert result.status == "error"
    assert result.message == "Authorization failed at step 2: Validate discovered ONT row"
    assert any(
        "validation failed after autofind refresh" in record.getMessage()
        and "UBNT-F9AA7344" in record.getMessage()
        and "Validate discovered ONT row" in record.getMessage()
        for record in caplog.records
    )


def test_authorize_autofind_recovers_when_serial_already_exists_on_olt(
    db_session, monkeypatch
) -> None:
    olt = OLTDevice(name="SPDC Huawei OLT", vendor="Huawei", model="MA5608T")
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)

    candidate = OltAutofindCandidate(
        olt_id=olt.id,
        fsp="0/1/3",
        serial_number="HWTC-8535819A",
        serial_hex="485754438535819A",
        vendor_id="HWTC",
        model="HG8546M",
        mac="",
        equipment_sn="",
        autofind_time="2026-04-06 09:00:00",
        is_active=True,
    )
    db_session.add(candidate)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.web_network_olts.get_olt_or_none",
        lambda *_args, **_kwargs: olt,
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh.authorize_ont",
        lambda *_args, **_kwargs: (
            False,
            "OLT rejected command: Failure: SN already exists",
            None,
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_write_reconciliation.verify_ont_authorized",
        lambda *_args, **_kwargs: SimpleNamespace(
            success=True,
            message="Verified ONT HWTC-8535819A on 0/1/3.",
            details={"ont_id": 9, "fsp": "0/1/3", "serial_number": "HWTC8535819A"},
        ),
    )
    monkeypatch.setattr(
        olt_authorization_workflow,
        "queue_post_authorization_follow_up",
        lambda *_args, **_kwargs: (True, "Queued follow-up.", "op-123"),
    )

    result = olt_authorization_workflow.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/3",
        "HWTC-8535819A",
    )

    assert result.success is True
    assert result.completed_authorization is True
    assert result.ont_id_on_olt == 9
    assert result.follow_up_operation_id == "op-123"
    assert any(
        "already registered on the OLT" in step.message for step in result.steps
    )

# ---------------------------------------------------------------------------
# Phase 1: Service-port SSH parsing and filtering
# ---------------------------------------------------------------------------


class TestServicePortParsing:
    """Test Huawei service-port table parsing."""

    def test_parse_standard_service_port_line(self) -> None:
        output = (
            "  27  201 common   gpon 0/2 /1  0    2     vlan  201  86   86   up\n"
            "  28  203 common   gpon 0/2 /1  0    3     vlan  203  86   86   down\n"
        )
        entries = _parse_service_port_table(output)
        assert len(entries) == 2
        assert entries[0].index == 27
        assert entries[0].vlan_id == 201
        assert entries[0].ont_id == 0
        assert entries[0].gem_index == 2
        assert entries[0].flow_type == "vlan"
        assert entries[0].state == "up"
        assert entries[1].index == 28
        assert entries[1].vlan_id == 203
        assert entries[1].state == "down"

    def test_parse_ignores_header_lines(self) -> None:
        output = (
            "INDEX VLAN ATTR TYPE F/S/P ONT GEM FLOW PARA RX TX STATE\n"
            "----- ---- ---- ---- ----- --- --- ---- ---- -- -- -----\n"
            "  27  201 common   gpon 0/2 /1  0    2     vlan  201  86   86   up\n"
        )
        entries = _parse_service_port_table(output)
        assert len(entries) == 1
        assert entries[0].index == 27

    def test_parse_empty_output(self) -> None:
        entries = _parse_service_port_table("")
        assert entries == []

    def test_parse_no_gpon_token(self) -> None:
        output = "  27  201 common   ethernet 0/2 /1  0    2     vlan  201  86   86   up\n"
        entries = _parse_service_port_table(output)
        assert len(entries) == 0


class TestServicePortFiltering:
    """Test get_service_ports_for_ont filtering logic."""

    def test_filter_by_ont_id(self) -> None:
        """Verify that get_service_ports_for_ont filters correctly."""
        all_ports = [
            ServicePortEntry(index=1, vlan_id=100, ont_id=0, gem_index=1, flow_type="vlan", flow_para="100", state="up"),
            ServicePortEntry(index=2, vlan_id=200, ont_id=1, gem_index=1, flow_type="vlan", flow_para="200", state="up"),
            ServicePortEntry(index=3, vlan_id=300, ont_id=0, gem_index=2, flow_type="vlan", flow_para="300", state="up"),
        ]
        # Simulate filtering (the actual function does SSH + filter, we test the filter logic)
        filtered = [p for p in all_ports if p.ont_id == 0]
        assert len(filtered) == 2
        assert all(p.ont_id == 0 for p in filtered)

    def test_clone_preserves_user_vlan_and_tag_transform(self, monkeypatch) -> None:
        from app.services.network.olt_ssh import create_service_ports

        olt = OLTDevice(name="Clone OLT", vendor="Huawei", model="MA5608T")

        sent_commands: list[str] = []

        class _FakeChannel:
            def send(self, _chars: str) -> None:
                return None

            def settimeout(self, _timeout: float) -> None:
                return None

        class _FakeTransport:
            def close(self) -> None:
                return None

        def _fake_run_huawei_cmd(_channel, command, prompt=None):  # noqa: ARG001
            sent_commands.append(command)
            return "success"

        monkeypatch.setattr(
            "app.services.network.olt_ssh._open_shell",
            lambda *_args, **_kwargs: (_FakeTransport(), _FakeChannel(), None),
        )
        monkeypatch.setattr(
            "app.services.network.olt_ssh._read_until_prompt",
            lambda *_args, **_kwargs: "#",
        )
        monkeypatch.setattr(
            "app.services.network.olt_ssh._run_huawei_cmd",
            _fake_run_huawei_cmd,
        )

        ok, msg = create_service_ports(
            olt,
            "0/2/1",
            7,
            cast(
                list[ServicePortEntry],
                [
                SimpleNamespace(
                    index=1,
                    vlan_id=201,
                    ont_id=3,
                    gem_index=2,
                    flow_type="vlan",
                    flow_para="101",
                    state="up",
                    tag_transform="translate",
                ),
                SimpleNamespace(
                    index=2,
                    vlan_id=301,
                    ont_id=3,
                    gem_index=4,
                    flow_type="vlan",
                    flow_para="untagged",
                    state="up",
                    tag_transform="default",
                ),
                ],
            ),
        )

        assert ok is True
        assert "Created 2 service-port(s)" in msg
        service_port_commands = [command for command in sent_commands if command.startswith("service-port vlan")]
        assert service_port_commands == [
            "service-port vlan 201 gpon 0/2/1 ont 7 gemport 2 multi-service user-vlan 101 tag-transform translate",
            "service-port vlan 301 gpon 0/2/1 ont 7 gemport 4 multi-service user-vlan untagged tag-transform default",
        ]


# ---------------------------------------------------------------------------
# Phase 1: VLAN chain validation
# ---------------------------------------------------------------------------


class TestVlanChainValidation:
    """Test VLAN chain validation logic."""

    def test_ont_not_found_returns_error(self, db_session) -> None:
        result = validate_chain(db_session, str(uuid.uuid4()))
        assert not result.valid
        assert any(w.level == "error" for w in result.warnings)

    def test_ont_without_assignment_returns_info(self, db_session) -> None:
        ont = OntUnit(serial_number="TEST-VLAN-001")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        result = validate_chain(db_session, str(ont.id))
        assert result.valid
        assert any("No active assignment" in w.message for w in result.warnings)

    def test_vlan_chain_result_structure(self) -> None:
        result = VlanChainResult(ont_id="test-id")
        assert result.valid is True
        assert result.desired_vlans == []
        assert result.actual_vlans == []
        assert result.warnings == []

    def test_vlan_chain_warning_fields(self) -> None:
        warning = VlanChainWarning("warning", "VLAN 100 missing")
        assert warning.level == "warning"
        assert warning.message == "VLAN 100 missing"


# ---------------------------------------------------------------------------
# Phase 3: Huawei command generation
# ---------------------------------------------------------------------------


class TestHuaweiCommandGenerator:
    """Test CLI command generation from provisioning specs."""

    def _make_context(
        self,
        *,
        frame: int = 0,
        slot: int = 2,
        port: int = 1,
        ont_id: int = 5,
        olt_name: str = "Test OLT",
        subscriber_code: str = "100014919",
        subscriber_name: str = "Demo User",
    ) -> OntProvisioningContext:
        return OntProvisioningContext(
            frame=frame,
            slot=slot,
            port=port,
            ont_id=ont_id,
            olt_name=olt_name,
            subscriber_code=subscriber_code,
            subscriber_name=subscriber_name,
        )

    def test_service_port_commands_generated(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(
            wan_services=[
                WanServiceSpec(service_type="internet", vlan_id=201, gem_index=2),
                WanServiceSpec(service_type="iptv", vlan_id=203, gem_index=3),
            ]
        )
        result = HuaweiCommandGenerator.generate_service_port_commands(spec, ctx)
        assert len(result) == 1
        assert result[0].step == "Create Service Ports"
        assert len(result[0].commands) == 2
        assert "vlan 201" in result[0].commands[0]
        assert "ont 5" in result[0].commands[0]
        assert "gemport 2" in result[0].commands[0]
        assert "0/2/1" in result[0].commands[0]
        assert "vlan 203" in result[0].commands[1]

    def test_service_port_commands_empty_when_no_wan_services(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(wan_services=[])
        result = HuaweiCommandGenerator.generate_service_port_commands(spec, ctx)
        assert result == []

    def test_iphost_dhcp_commands(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(mgmt_vlan_tag=100, mgmt_ip_mode="dhcp")
        result = HuaweiCommandGenerator.generate_iphost_commands(spec, ctx)
        assert len(result) == 1
        assert result[0].step == "Configure Management IP"
        cmds = result[0].commands
        assert any("interface gpon 0/2" in c for c in cmds)
        assert any("dhcp vlan 100" in c for c in cmds)

    def test_iphost_static_commands(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(
            mgmt_vlan_tag=100,
            mgmt_ip_mode="static",
            mgmt_ip_address="192.168.1.50",
            mgmt_subnet="255.255.255.0",
            mgmt_gateway="192.168.1.1",
        )
        result = HuaweiCommandGenerator.generate_iphost_commands(spec, ctx)
        assert len(result) == 1
        cmds = " ".join(result[0].commands)
        assert "static" in cmds
        assert "192.168.1.50" in cmds
        assert "255.255.255.0" in cmds
        assert "192.168.1.1" in cmds

    def test_iphost_skipped_when_no_mgmt_vlan(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(mgmt_vlan_tag=None)
        result = HuaweiCommandGenerator.generate_iphost_commands(spec, ctx)
        assert result == []

    def test_tr069_binding_commands(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(tr069_profile_id=3)
        result = HuaweiCommandGenerator.generate_tr069_binding_commands(spec, ctx)
        assert len(result) == 1
        assert result[0].step == "Bind TR-069 Profile"
        cmds = " ".join(result[0].commands)
        assert "tr069-server-config 1 5 profile-id 3" in cmds

    def test_tr069_skipped_when_no_profile(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(tr069_profile_id=None)
        result = HuaweiCommandGenerator.generate_tr069_binding_commands(spec, ctx)
        assert result == []

    def test_full_provisioning_generates_all_steps(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(
            wan_services=[
                WanServiceSpec(service_type="internet", vlan_id=201, gem_index=2),
            ],
            mgmt_vlan_tag=100,
            mgmt_ip_mode="dhcp",
            tr069_profile_id=1,
        )
        result = HuaweiCommandGenerator.generate_full_provisioning(spec, ctx)
        steps = [cs.step for cs in result]
        assert "Create Service Ports" in steps
        assert "Configure Management IP" in steps
        assert "Bind TR-069 Profile" in steps

    def test_full_provisioning_empty_spec(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec()
        result = HuaweiCommandGenerator.generate_full_provisioning(spec, ctx)
        assert result == []


class TestOntProvisioningContext:
    """Test provisioning context properties."""

    def test_fsp_format(self) -> None:
        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)
        assert ctx.fsp == "0/2/1"

    def test_frame_slot_format(self) -> None:
        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)
        assert ctx.frame_slot == "0/2"


class TestTemplateRendering:
    """Test PPPoE username template rendering."""

    def test_subscriber_code_rendered(self) -> None:
        from app.services.network.olt_command_gen import _render_template

        ctx = OntProvisioningContext(
            frame=0, slot=2, port=1, ont_id=5,
            subscriber_code="100014919",
        )
        result = _render_template("{subscriber_code}", ctx)
        assert result == "100014919"

    def test_subscriber_name_rendered(self) -> None:
        from app.services.network.olt_command_gen import _render_template

        ctx = OntProvisioningContext(
            frame=0, slot=2, port=1, ont_id=5,
            subscriber_name="John Doe",
        )
        result = _render_template("{subscriber_name}", ctx)
        assert result == "John Doe"

    def test_ont_id_rendered(self) -> None:
        from app.services.network.olt_command_gen import _render_template

        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=42)
        result = _render_template("user-{ont_id}", ctx)
        assert result == "user-42"

    def test_empty_template_unchanged(self) -> None:
        from app.services.network.olt_command_gen import _render_template

        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)
        assert _render_template("", ctx) == ""

    def test_no_placeholders_unchanged(self) -> None:
        from app.services.network.olt_command_gen import _render_template

        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)
        assert _render_template("static_user", ctx) == "static_user"


class TestBuildSpecFromProfile:
    """Test profile-to-spec conversion."""

    def test_builds_spec_from_mock_profile(self) -> None:
        wan_svc = SimpleNamespace(
            is_active=True,
            s_vlan=201,
            c_vlan=None,
            service_type=SimpleNamespace(value="internet"),
            gem_port_id=2,
            connection_type=SimpleNamespace(value="pppoe"),
            pppoe_username_template="{subscriber_code}@isp.ng",
            pppoe_static_password="secret123",
            cos_priority=None,
            nat_enabled=True,
            t_cont_profile=None,
        )
        profile = SimpleNamespace(
            wan_services=[wan_svc],
            mgmt_vlan_tag=100,
            mgmt_ip_mode=SimpleNamespace(value="dhcp"),
        )
        ctx = OntProvisioningContext(
            frame=0, slot=2, port=1, ont_id=5,
            subscriber_code="100014919",
        )
        spec = build_spec_from_profile(profile, ctx, tr069_profile_id=3)

        assert len(spec.wan_services) == 1
        assert spec.wan_services[0].vlan_id == 201
        assert spec.wan_services[0].gem_index == 2
        assert spec.wan_services[0].pppoe_username_template == "{subscriber_code}@isp.ng"
        assert spec.mgmt_vlan_tag == 100
        assert spec.mgmt_ip_mode == "dhcp"
        assert spec.tr069_profile_id == 3

    def test_builds_translate_service_port_with_distinct_user_vlan(self) -> None:
        wan_svc = SimpleNamespace(
            is_active=True,
            s_vlan=201,
            c_vlan=101,
            vlan_mode=SimpleNamespace(value="translate"),
            service_type=SimpleNamespace(value="internet"),
            gem_port_id=2,
            connection_type=SimpleNamespace(value="pppoe"),
            pppoe_username_template=None,
            pppoe_password_mode=None,
            pppoe_static_password=None,
            cos_priority=None,
            nat_enabled=True,
            t_cont_profile=None,
        )
        profile = SimpleNamespace(
            wan_services=[wan_svc],
            mgmt_vlan_tag=None,
            mgmt_ip_mode=None,
        )
        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)

        spec = build_spec_from_profile(profile, ctx)
        commands = HuaweiCommandGenerator.generate_service_port_commands(spec, ctx)

        assert spec.wan_services[0].vlan_id == 201
        assert spec.wan_services[0].user_vlan == 101
        assert "service-port vlan 201" in commands[0].commands[0]
        assert "user-vlan 101" in commands[0].commands[0]

    def test_build_spec_decrypts_static_pppoe_password(self) -> None:
        wan_svc = SimpleNamespace(
            is_active=True,
            s_vlan=201,
            c_vlan=None,
            vlan_mode=SimpleNamespace(value=VlanMode.tagged.value),
            service_type=SimpleNamespace(value="internet"),
            gem_port_id=2,
            connection_type=SimpleNamespace(value="pppoe"),
            pppoe_username_template="{subscriber_code}",
            pppoe_password_mode=SimpleNamespace(value=PppoePasswordMode.static.value),
            pppoe_static_password="plain:secret123",
            cos_priority=None,
            nat_enabled=True,
            t_cont_profile=None,
        )
        profile = SimpleNamespace(
            wan_services=[wan_svc],
            mgmt_vlan_tag=None,
            mgmt_ip_mode=None,
        )
        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)

        spec = build_spec_from_profile(profile, ctx)

        assert spec.wan_services[0].pppoe_password == "secret123"
        assert spec.wan_services[0].pppoe_password_mode == "static"

    def test_inactive_wan_services_skipped(self) -> None:
        wan_svc = SimpleNamespace(
            is_active=False,
            s_vlan=201,
            c_vlan=None,
            service_type=SimpleNamespace(value="internet"),
            gem_port_id=2,
            connection_type=SimpleNamespace(value="pppoe"),
            pppoe_username_template="",
            pppoe_static_password="",
            cos_priority=None,
            nat_enabled=True,
        )
        profile = SimpleNamespace(
            wan_services=[wan_svc],
            mgmt_vlan_tag=None,
            mgmt_ip_mode=None,
        )
        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)
        spec = build_spec_from_profile(profile, ctx)
        assert len(spec.wan_services) == 0

    def test_wan_service_without_vlan_skipped(self) -> None:
        wan_svc = SimpleNamespace(
            is_active=True,
            s_vlan=None,
            c_vlan=None,
            service_type=SimpleNamespace(value="internet"),
            gem_port_id=2,
            connection_type=SimpleNamespace(value="pppoe"),
            pppoe_username_template="",
            pppoe_static_password="",
            cos_priority=None,
            nat_enabled=True,
        )
        profile = SimpleNamespace(
            wan_services=[wan_svc],
            mgmt_vlan_tag=None,
            mgmt_ip_mode=None,
        )
        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)
        spec = build_spec_from_profile(profile, ctx)
        assert len(spec.wan_services) == 0


# ---------------------------------------------------------------------------
# Phase 3: OLT profile SSH output parsing
# ---------------------------------------------------------------------------


class TestOltProfileParsing:
    """Test OLT profile table output parsing."""

    def test_parse_profile_table_standard(self) -> None:
        output = (
            "  Profile-ID  Profile-name\n"
            "  ----------  ----------------------\n"
            "  1           default\n"
            "  2           residential-gpon\n"
            "  3           business-gpon\n"
        )
        entries = _parse_profile_table(output)
        assert len(entries) == 3
        assert entries[0].profile_id == 1
        assert entries[0].name == "default"
        assert entries[1].profile_id == 2
        assert entries[1].name == "residential-gpon"
        assert entries[2].profile_id == 3
        assert entries[2].name == "business-gpon"

    def test_parse_profile_table_empty(self) -> None:
        entries = _parse_profile_table("")
        assert entries == []

    def test_parse_profile_table_separator_lines_ignored(self) -> None:
        output = (
            "---------- ----------------------\n"
            "============================================\n"
            "  1  default\n"
        )
        entries = _parse_profile_table(output)
        assert len(entries) == 1

    def test_olt_profile_entry_defaults(self) -> None:
        entry = OltProfileEntry(profile_id=1, name="test")
        assert entry.type == ""
        assert entry.binding_count == 0
        assert entry.extra == {}


# ---------------------------------------------------------------------------
# Web service wrappers (Phase 2)
# ---------------------------------------------------------------------------


class TestWebNetworkOntActionsWrappers:
    """Test that the new web service wrapper functions exist and have correct signatures."""

    def test_execute_omci_reboot_exists(self) -> None:
        from app.services.web_network_ont_actions import execute_omci_reboot

        assert callable(execute_omci_reboot)

    def test_configure_management_ip_exists(self) -> None:
        from app.services.web_network_ont_actions import configure_management_ip

        assert callable(configure_management_ip)

    def test_fetch_iphost_config_exists(self) -> None:
        from app.services.web_network_ont_actions import fetch_iphost_config

        assert callable(fetch_iphost_config)

    def test_bind_tr069_profile_exists(self) -> None:
        from app.services.web_network_ont_actions import bind_tr069_profile

        assert callable(bind_tr069_profile)


class TestWebNetworkServicePortsWrappers:
    """Test service-port web service functions."""

    def test_parse_ont_id_on_olt_accepts_generic_prefixed_numeric_id(self) -> None:
        from app.services.web_network_service_ports import _parse_ont_id_on_olt

        assert _parse_ont_id_on_olt("generic:5") == 5

    def test_normalize_fsp_strips_pon_prefix(self) -> None:
        from app.services.web_network_service_ports import _normalize_fsp

        assert _normalize_fsp("pon-0/2/1") == "0/2/1"

    def test_list_context_ont_not_found(self, db_session) -> None:
        from app.services.web_network_service_ports import list_context

        ctx = list_context(db_session, str(uuid.uuid4()))
        assert ctx["error"] is not None
        assert "not found" in ctx["error"].lower()

    def test_list_context_ont_no_assignment(self, db_session) -> None:
        from app.services.web_network_service_ports import list_context

        ont = OntUnit(serial_number="TEST-SP-001")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        ctx = list_context(db_session, str(ont.id))
        assert ctx["error"] is not None
        assert "assignment" in ctx["error"].lower() or "mapping" in ctx["error"].lower()

    def test_list_context_includes_reference_onts_on_same_olt(self, db_session, monkeypatch) -> None:
        from app.services.web_network_service_ports import list_context

        olt = OLTDevice(name="Reference OLT", vendor="Huawei", model="MA5608T")
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        pon_a = PonPort(olt_id=olt.id, name="0/2/1")
        pon_b = PonPort(olt_id=olt.id, name="0/2/2")
        db_session.add_all([pon_a, pon_b])
        db_session.commit()
        db_session.refresh(pon_a)
        db_session.refresh(pon_b)

        target = OntUnit(serial_number="TARGET-ONT", board="0/2", port="1", external_id="7")
        ref = OntUnit(serial_number="REF-ONT", board="0/2", port="2", external_id="9")
        db_session.add_all([target, ref])
        db_session.commit()
        db_session.refresh(target)
        db_session.refresh(ref)

        db_session.add_all(
            [
                OntAssignment(ont_unit_id=target.id, pon_port_id=pon_a.id, active=True),
                OntAssignment(ont_unit_id=ref.id, pon_port_id=pon_b.id, active=True),
            ]
        )
        db_session.commit()

        monkeypatch.setattr(
            "app.services.web_network_service_ports.get_service_ports_for_ont",
            lambda *_args, **_kwargs: (True, "ok", []),
        )

        ctx = list_context(db_session, str(target.id))

        assert ctx["error"] is None
        assert ctx["reference_onts"] == [
            {
                "id": str(ref.id),
                "label": "REF-ONT | ONT-ID 9 | 0/2/2",
            }
        ]

    def test_list_context_accepts_prefixed_pon_name(self, db_session, monkeypatch) -> None:
        from app.services.web_network_service_ports import list_context

        olt = OLTDevice(name="Prefixed OLT", vendor="Huawei", model="MA5608T")
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        pon = PonPort(olt_id=olt.id, name="pon-0/2/1")
        db_session.add(pon)
        db_session.commit()
        db_session.refresh(pon)

        ont = OntUnit(serial_number="TARGET-ONT", external_id="generic:7")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        db_session.add(OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True))
        db_session.commit()

        captured: dict[str, object] = {}

        def _fake_get_service_ports_for_ont(_olt, fsp, ont_id):
            captured["fsp"] = fsp
            captured["ont_id"] = ont_id
            return True, "ok", []

        monkeypatch.setattr(
            "app.services.web_network_service_ports.get_service_ports_for_ont",
            _fake_get_service_ports_for_ont,
        )

        ctx = list_context(db_session, str(ont.id))

        assert ctx["error"] is None
        assert captured == {"fsp": "0/2/1", "ont_id": 7}

    def test_handle_create_no_olt_context(self, db_session) -> None:
        from app.services.web_network_service_ports import handle_create

        ok, msg = handle_create(db_session, str(uuid.uuid4()), 201, 1)
        assert not ok
        assert "OLT" in msg or "resolve" in msg.lower()

    def test_handle_create_forwards_user_vlan_and_transform(self, db_session, monkeypatch) -> None:
        from app.services.web_network_service_ports import handle_create

        olt = OLTDevice(name="SP Create OLT", vendor="Huawei", model="MA5608T")
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        pon = PonPort(olt_id=olt.id, name="0/2/1")
        db_session.add(pon)
        db_session.commit()
        db_session.refresh(pon)

        ont = OntUnit(serial_number="TEST-SP-CREATE-001", board="0/2", port="1", external_id="7")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        db_session.add(OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True))
        db_session.commit()

        captured: dict[str, object] = {}

        def _fake_create_single_service_port(olt_obj, fsp, olt_ont_id, gem_index, vlan_id, *, user_vlan=None, tag_transform="translate"):
            captured.update(
                {
                    "olt_id": str(olt_obj.id),
                    "fsp": fsp,
                    "olt_ont_id": olt_ont_id,
                    "gem_index": gem_index,
                    "vlan_id": vlan_id,
                    "user_vlan": user_vlan,
                    "tag_transform": tag_transform,
                }
            )
            return True, "created"

        monkeypatch.setattr(
            "app.services.web_network_service_ports.create_single_service_port",
            _fake_create_single_service_port,
        )

        ok, msg = handle_create(
            db_session,
            str(ont.id),
            201,
            3,
            user_vlan=101,
            tag_transform="transparent",
        )

        assert ok is True
        assert msg == "created"
        assert captured == {
            "olt_id": str(olt.id),
            "fsp": "0/2/1",
            "olt_ont_id": 7,
            "gem_index": 3,
            "vlan_id": 201,
            "user_vlan": 101,
            "tag_transform": "transparent",
        }

    def test_handle_delete_no_olt_context(self, db_session) -> None:
        from app.services.web_network_service_ports import handle_delete

        ok, msg = handle_delete(db_session, str(uuid.uuid4()), 27)
        assert not ok

    def test_handle_clone_no_olt_context(self, db_session) -> None:
        from app.services.web_network_service_ports import handle_clone

        ok, msg = handle_clone(db_session, str(uuid.uuid4()), str(uuid.uuid4()))
        assert not ok


class TestWebNetworkOltProfiles:
    """Test OLT profile web service functions."""

    def test_line_profiles_olt_not_found(self, db_session) -> None:
        from app.services.web_network_olt_profiles import line_profiles_context

        ctx = line_profiles_context(db_session, str(uuid.uuid4()))
        assert ctx["error"] is not None
        assert ctx["line_profiles"] == []

    def test_tr069_profiles_olt_not_found(self, db_session) -> None:
        from app.services.web_network_olt_profiles import tr069_profiles_context

        ctx = tr069_profiles_context(db_session, str(uuid.uuid4()))
        assert ctx["error"] is not None
        assert ctx["tr069_profiles"] == []

    def test_command_preview_ont_not_found(self, db_session) -> None:
        from app.services.web_network_olt_profiles import command_preview_context

        ctx = command_preview_context(db_session, str(uuid.uuid4()), str(uuid.uuid4()))
        assert ctx["error"] is not None


class TestWebNetworkOltsMigration:
    def test_provision_ont_service_ports_targets_explicit_ont_id(self, db_session, monkeypatch) -> None:
        from app.services.web_network_olts import provision_ont_service_ports

        olt = OLTDevice(name="Neighbor Learning OLT", vendor="Huawei", model="MA5608T")
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        entries = [
            SimpleNamespace(ont_id=11, vlan_id=201, gem_index=1),
            SimpleNamespace(ont_id=11, vlan_id=202, gem_index=2),
            SimpleNamespace(ont_id=15, vlan_id=300, gem_index=1),
        ]
        captured: dict[str, object] = {}

        monkeypatch.setattr(
            "app.services.web_network_olts.olt_ssh_service.get_service_ports",
            lambda *_args, **_kwargs: (True, "ok", entries),
        )

        def _fake_create_service_ports(olt_obj, fsp, ont_id, reference_ports):
            captured.update(
                {
                    "olt_id": str(olt_obj.id),
                    "fsp": fsp,
                    "ont_id": ont_id,
                    "reference_ont_ids": [port.ont_id for port in reference_ports],
                    "reference_vlans": [port.vlan_id for port in reference_ports],
                }
            )
            return True, "created"

        monkeypatch.setattr(
            "app.services.web_network_olts.olt_ssh_service.create_service_ports",
            _fake_create_service_ports,
        )

        ok, msg = provision_ont_service_ports(db_session, str(olt.id), "0/2/1", 7)

        assert ok is True
        assert msg == "created"
        assert captured == {
            "olt_id": str(olt.id),
            "fsp": "0/2/1",
            "ont_id": 7,
            "reference_ont_ids": [11, 11],
            "reference_vlans": [201, 202],
        }

    def test_authorize_autofind_skips_clone_when_ont_id_unknown(self, db_session, monkeypatch) -> None:
        from app.services.web_network_olts import authorize_autofind_ont

        monkeypatch.setattr(
            "app.services.network.olt_authorization_workflow.authorize_autofind_ont",
            lambda *_args, **_kwargs: SimpleNamespace(
                success=True,
                status="warning",
                message="Authorization completed on OLT, but follow-up is pending.",
            ),
        )

        ok, status, msg = authorize_autofind_ont(
            db_session,
            str(uuid.uuid4()),
            "0/2/1",
            "48575443ABCDEF01",
        )

        assert ok is True
        assert status == "warning"
        assert msg == "Authorization completed on OLT, but follow-up is pending."

    def test_authorize_autofind_delegates_to_authorization_workflow(self, db_session, monkeypatch) -> None:
        from app.services.web_network_olts import authorize_autofind_ont

        captured: dict[str, object] = {}

        def _fake_authorize_workflow(db, olt_id, fsp, serial_number):
            captured.update(
                {
                    "db": db,
                    "olt_id": olt_id,
                    "fsp": fsp,
                    "serial_number": serial_number,
                }
            )
            return SimpleNamespace(
                success=True,
                status="success",
                message="Queued post-authorization sync and ACS bind in the background.",
            )

        monkeypatch.setattr(
            "app.services.network.olt_authorization_workflow.authorize_autofind_ont",
            _fake_authorize_workflow,
        )

        ok, status, msg = authorize_autofind_ont(
            db_session,
            "olt-123",
            "0/2/1",
            "48575443ABCDEF02",
        )

        assert ok is True
        assert status == "success"
        assert msg == "Queued post-authorization sync and ACS bind in the background."
        assert captured == {
            "db": db_session,
            "olt_id": "olt-123",
            "fsp": "0/2/1",
            "serial_number": "48575443ABCDEF02",
        }

    def test_command_preview_profile_not_found(self, db_session) -> None:
        from app.services.web_network_olt_profiles import command_preview_context

        ont = OntUnit(serial_number="TEST-CP-001", board="0/2", port="1", external_id="5")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        olt = OLTDevice(name="Preview OLT", vendor="Huawei", model="MA5608T")
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        pon = PonPort(olt_id=olt.id, name="0/2/1")
        db_session.add(pon)
        db_session.commit()
        db_session.refresh(pon)

        assignment = OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        db_session.add(assignment)
        db_session.commit()

        ctx = command_preview_context(db_session, str(ont.id), str(uuid.uuid4()))
        assert ctx["error"] is not None
        assert "profile" in ctx["error"].lower() or "not found" in ctx["error"].lower()


class TestGetProfileTemplates:
    """Test ONT profile template listing service."""

    def test_get_profile_templates_empty(self, db_session) -> None:
        from app.services.web_network_onts import get_profile_templates

        profiles = get_profile_templates(db_session)
        # May have existing profiles from other tests, just verify it returns a list
        assert isinstance(profiles, list)

    def test_get_profile_templates_returns_active_only(self, db_session) -> None:
        from app.services.web_network_onts import get_profile_templates

        org = _create_business_subscriber(db_session)
        active = OntProvisioningProfile(
            owner_subscriber_id=org.id,
            name="Active Profile Test",
            is_active=True,
        )
        inactive = OntProvisioningProfile(
            owner_subscriber_id=org.id,
            name="Inactive Profile Test",
            is_active=False,
        )
        db_session.add_all([active, inactive])
        db_session.commit()

        profiles = get_profile_templates(db_session)
        profile_names = [p.name for p in profiles]
        assert "Active Profile Test" in profile_names
        assert "Inactive Profile Test" not in profile_names


# ---------------------------------------------------------------------------
# Route registration (all phases)
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    """Verify all new routes are registered on the router."""

    def test_new_routes_registered(self) -> None:
        from app.web.admin.network_olts_onts import router

        route_paths = [route.path for route in router.routes if isinstance(route, Route)]

        # Phase 1: Service-port routes
        assert "/network/onts/{ont_id}/service-ports" in route_paths
        assert "/network/onts/{ont_id}/service-ports/create" in route_paths
        assert "/network/onts/{ont_id}/service-ports/{index}/delete" in route_paths
        assert "/network/onts/{ont_id}/service-ports/clone" in route_paths

        # Phase 2: OMCI / management IP / TR-069 routes
        assert "/network/onts/{ont_id}/actions/omci-reboot" in route_paths
        assert "/network/onts/{ont_id}/actions/configure-mgmt-ip" in route_paths
        assert "/network/onts/{ont_id}/actions/bind-tr069-profile" in route_paths
        assert "/network/onts/{ont_id}/iphost-config" in route_paths
        assert "/network/onts/{ont_id}/location-details" in route_paths
        assert "/network/onts/{ont_id}/device-info" in route_paths
        assert "/network/onts/{ont_id}/gpon-channel" in route_paths

        # Phase 3: OLT profile routes remain, provisioning UI routes do not
        assert "/network/olts/{olt_id}/profiles/line" in route_paths
        assert "/network/olts/{olt_id}/profiles/tr069" in route_paths

        assert "/network/onts/{ont_id}/provisioning-preview" not in route_paths
        assert "/network/onts/{ont_id}/preflight" not in route_paths
        assert "/network/onts/{ont_id}/provision" not in route_paths
        assert "/network/onts/{ont_id}/provision-status" not in route_paths


class TestProvisioningUiTemplates:
    def test_provisioning_widget_replaced_with_manual_operations_notice(self) -> None:
        template = Path("templates/admin/network/onts/_provision_action.html").read_text()

        assert "Manual ONT Operations" in template
        assert "Coordinated provisioning is not available" in template
        assert "clone service-ports" in template

    def test_ont_detail_template_includes_live_location_details_card(self) -> None:
        template = Path("templates/admin/network/onts/detail.html").read_text()

        assert 'card("Location Details"' in template
        assert 'hx-get="/admin/network/onts/{{ ont.id }}/location-details"' in template
        assert "ODB (Splitter)" in template
        assert "Address or comment" in template
        assert "Contact" in template

    def test_ont_detail_template_includes_live_device_and_gpon_modals(self) -> None:
        template = Path("templates/admin/network/onts/detail.html").read_text()

        assert 'hx-get="/admin/network/onts/{{ ont.id }}/device-info"' in template
        assert 'hx-get="/admin/network/onts/{{ ont.id }}/gpon-channel"' in template
        assert "Board" in template
        assert "GPON Channel" in template


class TestOntLocationDetailsHelpers:
    def test_resolve_splitter_port_id_by_number(self, db_session) -> None:
        from app.web.admin.network_onts import _resolve_splitter_port_id

        splitter = Splitter(name="ODB-A", splitter_ratio="1:8")
        db_session.add(splitter)
        db_session.commit()
        db_session.refresh(splitter)

        db_session.add_all(
            [
                SplitterPort(splitter_id=splitter.id, port_number=1),
                SplitterPort(splitter_id=splitter.id, port_number=8),
            ]
        )
        db_session.commit()

        resolved = _resolve_splitter_port_id(
            db_session,
            splitter_id=splitter.id,
            splitter_port_number=8,
        )

        assert resolved is not None

    def test_resolve_splitter_port_id_rejects_port_without_splitter(self, db_session) -> None:
        from app.web.admin.network_onts import _resolve_splitter_port_id

        try:
            _resolve_splitter_port_id(
                db_session,
                splitter_id=None,
                splitter_port_number=3,
            )
        except ValueError as exc:
            assert "Select an ODB" in str(exc)
        else:
            raise AssertionError("Expected ValueError when ODB Port is set without splitter")

    def test_provisioning_widget_has_no_orchestration_controls(self) -> None:
        template = Path("templates/admin/network/onts/_provision_action.html").read_text()

        assert "Preflight Checklist" not in template
        assert "/preflight" not in template
        assert "/provisioning-preview" not in template
        assert "/provision-status" not in template

    def test_ont_form_describes_supported_external_id_formats(self) -> None:
        template = Path("templates/admin/network/onts/form.html").read_text()

        assert "huawei:4194320640.5" in template
        assert "resolved ONT-ID" in template

    def test_assignment_ui_allows_blank_pon_port_for_tr069_only(self) -> None:
        template = Path("templates/admin/network/onts/assign.html").read_text()

        assert "Leave blank for TR-069-only assignment" in template
        assert "TR-069-only onboarding" in template
        assert 'name="pon_port_id" id="pon_port_id" required' not in template

    def test_ont_list_offers_tr069_import_shortcut(self) -> None:
        template = Path("templates/admin/network/onts/index.html").read_text()

        assert "/admin/network/tr069?only_unlinked=true" in template
        assert "Import From TR-069" in template


class TestOntAssignmentValidation:
    def test_validate_form_values_allows_blank_pon_port(self) -> None:
        from app.services import web_network_ont_assignments as svc

        error = svc.validate_form_values(
            {
                "pon_port_id": "",
                "account_id": "acct-1",
                "service_address_id": None,
                "notes": None,
            }
        )

        assert error is None


# ---------------------------------------------------------------------------
# OLT SSH function existence checks
# ---------------------------------------------------------------------------


class TestOltSshFunctionExistence:
    """Verify all new SSH functions are importable."""

    def test_delete_service_port_importable(self) -> None:
        from app.services.network.olt_ssh import delete_service_port

        assert callable(delete_service_port)

    def test_create_single_service_port_importable(self) -> None:
        from app.services.network.olt_ssh import create_single_service_port

        assert callable(create_single_service_port)

    def test_get_service_ports_for_ont_importable(self) -> None:
        from app.services.network.olt_ssh import get_service_ports_for_ont

        assert callable(get_service_ports_for_ont)

    def test_configure_ont_iphost_importable(self) -> None:
        from app.services.network.olt_ssh import configure_ont_iphost

        assert callable(configure_ont_iphost)

    def test_get_ont_iphost_config_importable(self) -> None:
        from app.services.network.olt_ssh import get_ont_iphost_config

        assert callable(get_ont_iphost_config)

    def test_reboot_ont_omci_importable(self) -> None:
        from app.services.network.olt_ssh import reboot_ont_omci

        assert callable(reboot_ont_omci)

    def test_bind_tr069_server_profile_importable(self) -> None:
        from app.services.network.olt_ssh import bind_tr069_server_profile

        assert callable(bind_tr069_server_profile)

    def test_get_line_profiles_importable(self) -> None:
        from app.services.network.olt_ssh import get_line_profiles

        assert callable(get_line_profiles)

    def test_get_service_profiles_importable(self) -> None:
        from app.services.network.olt_ssh import get_service_profiles

        assert callable(get_service_profiles)

    def test_get_tr069_server_profiles_importable(self) -> None:
        from app.services.network.olt_ssh import get_tr069_server_profiles

        assert callable(get_tr069_server_profiles)


# ---------------------------------------------------------------------------
# OltCommandSet and dataclass tests
# ---------------------------------------------------------------------------


class TestOltCommandSetDataclass:
    """Test OltCommandSet fields."""

    def test_defaults(self) -> None:
        cs = OltCommandSet(step="Test", commands=["cmd1"])
        assert cs.description == ""
        assert cs.requires_config_mode is True

    def test_custom_fields(self) -> None:
        cs = OltCommandSet(
            step="Custom",
            commands=["a", "b"],
            description="Some desc",
            requires_config_mode=False,
        )
        assert cs.step == "Custom"
        assert len(cs.commands) == 2
        assert cs.description == "Some desc"
        assert cs.requires_config_mode is False
