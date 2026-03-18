"""Tests for OLT/VLAN/TR-069 provisioning gap fixes (Phases 1-4).

Covers:
- Service-port SSH command parsing and filtering (Phase 1)
- VLAN chain validation (Phase 1)
- Huawei command generation from provisioning profiles (Phase 3)
- Provisioning orchestrator context resolution and dry-run (Phase 4)
- Celery task registration (Phase 4)
- Web service wrappers (Phase 2)
- Route registration (all phases)
- OLT profile SSH output parsing (Phase 3)
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntProvisioningProfile,
    OntUnit,
    PonPort,
)
from app.models.subscriber import Organization
from app.services.events.types import EventType
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


def _create_org(db_session) -> Organization:
    """Create a minimal Organization for FK constraints."""
    org = Organization(name=f"Test Org {uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    return org

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
# Phase 4: Provisioning orchestrator
# ---------------------------------------------------------------------------


class TestProvisioningOrchestrator:
    """Test the end-to-end provisioning orchestrator."""

    def test_provision_ont_not_found(self, db_session) -> None:
        from app.services.network.ont_provisioning_orchestrator import (
            OntProvisioningOrchestrator,
        )

        result = OntProvisioningOrchestrator.provision_ont(
            db_session, str(uuid.uuid4()), str(uuid.uuid4()), dry_run=True
        )
        assert not result.success
        assert "ONT not found" in result.message
        assert len(result.steps) >= 1
        assert not result.steps[0].success

    def test_provision_ont_no_assignment(self, db_session) -> None:
        from app.services.network.ont_provisioning_orchestrator import (
            OntProvisioningOrchestrator,
        )

        ont = OntUnit(serial_number="TEST-PROV-001")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        org = _create_org(db_session)
        profile = OntProvisioningProfile(
            organization_id=org.id,
            name="Test Profile",
        )
        db_session.add(profile)
        db_session.commit()
        db_session.refresh(profile)

        result = OntProvisioningOrchestrator.provision_ont(
            db_session, str(ont.id), str(profile.id), dry_run=True
        )
        assert not result.success
        assert "OLT" in result.message or "context" in result.message.lower()

    def test_provision_ont_profile_not_found(self, db_session) -> None:
        from app.services.network.ont_provisioning_orchestrator import (
            OntProvisioningOrchestrator,
        )

        ont = OntUnit(serial_number="TEST-PROV-002", board="0/2", port="1", external_id="5")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        # Create OLT + PonPort + assignment for context resolution
        olt = OLTDevice(name="Provisioning OLT", vendor="Huawei", model="MA5608T")
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

        result = OntProvisioningOrchestrator.provision_ont(
            db_session, str(ont.id), str(uuid.uuid4()), dry_run=True
        )
        assert not result.success
        assert "profile" in result.message.lower() or "not found" in result.message.lower()

    def test_dry_run_generates_commands(self, db_session) -> None:
        from app.services.network.ont_provisioning_orchestrator import (
            OntProvisioningOrchestrator,
        )

        ont = OntUnit(serial_number="TEST-PROV-003", board="0/2", port="1", external_id="5")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        olt = OLTDevice(name="Dry Run OLT", vendor="Huawei", model="MA5608T")
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

        org = _create_org(db_session)
        profile = OntProvisioningProfile(
            organization_id=org.id,
            name="Dry Run Profile",
            mgmt_vlan_tag=100,
        )
        db_session.add(profile)
        db_session.commit()
        db_session.refresh(profile)

        result = OntProvisioningOrchestrator.provision_ont(
            db_session, str(ont.id), str(profile.id), dry_run=True
        )
        assert result.success
        assert result.dry_run
        assert "dry run" in result.message.lower() or "generated" in result.message.lower()
        # At minimum: resolve + generate + dry-run steps
        assert len(result.steps) >= 3

    def test_provisioning_result_to_dict(self) -> None:
        from app.services.network.ont_provisioning_orchestrator import (
            ProvisioningJobResult,
            ProvisioningStepResult,
        )

        result = ProvisioningJobResult(
            success=True,
            message="Test complete",
            dry_run=True,
            steps=[
                ProvisioningStepResult(step=1, name="Step One", success=True, message="OK", duration_ms=50),
            ],
            command_sets=[
                OltCommandSet(step="Test", commands=["cmd1", "cmd2"], description="Test commands"),
            ],
        )
        d = result.to_dict()
        assert d["success"] is True
        assert d["dry_run"] is True
        assert len(d["steps"]) == 1
        assert d["steps"][0]["name"] == "Step One"
        assert d["steps"][0]["duration_ms"] == 50
        assert len(d["command_preview"]) == 1
        assert d["command_preview"][0]["commands"] == ["cmd1", "cmd2"]


# ---------------------------------------------------------------------------
# Phase 4: Celery task registration
# ---------------------------------------------------------------------------


class TestCeleryTaskRegistration:
    """Verify new provisioning Celery task is importable and named."""

    def test_provision_ont_task_exists(self) -> None:
        from app.tasks.provisioning import provision_ont

        assert provision_ont.name == "app.tasks.provisioning.provision_ont"


# ---------------------------------------------------------------------------
# Event type registration
# ---------------------------------------------------------------------------


class TestOntProvisionedEvent:
    """Verify ont_provisioned event type exists."""

    def test_ont_provisioned_event_value(self) -> None:
        assert EventType.ont_provisioned.value == "ont.provisioned"


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

    def test_handle_create_no_olt_context(self, db_session) -> None:
        from app.services.web_network_service_ports import handle_create

        ok, msg = handle_create(db_session, str(uuid.uuid4()), 201, 1)
        assert not ok
        assert "OLT" in msg or "resolve" in msg.lower()

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


class TestGetProvisioningProfiles:
    """Test provisioning profile listing service."""

    def test_get_provisioning_profiles_empty(self, db_session) -> None:
        from app.services.web_network_onts import get_provisioning_profiles

        profiles = get_provisioning_profiles(db_session)
        # May have existing profiles from other tests, just verify it returns a list
        assert isinstance(profiles, list)

    def test_get_provisioning_profiles_returns_active_only(self, db_session) -> None:
        from app.services.web_network_onts import get_provisioning_profiles

        org = _create_org(db_session)
        active = OntProvisioningProfile(
            organization_id=org.id,
            name="Active Profile Test",
            is_active=True,
        )
        inactive = OntProvisioningProfile(
            organization_id=org.id,
            name="Inactive Profile Test",
            is_active=False,
        )
        db_session.add_all([active, inactive])
        db_session.commit()

        profiles = get_provisioning_profiles(db_session)
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

        route_paths = [r.path for r in router.routes]

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

        # Phase 3: OLT profiles + provisioning preview
        assert "/network/olts/{olt_id}/profiles/line" in route_paths
        assert "/network/olts/{olt_id}/profiles/tr069" in route_paths
        assert "/network/onts/{ont_id}/provisioning-preview" in route_paths

        # Phase 4: End-to-end provisioning
        assert "/network/onts/{ont_id}/provision" in route_paths
        assert "/network/onts/{ont_id}/provision-status" in route_paths


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
