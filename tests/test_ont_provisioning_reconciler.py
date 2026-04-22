"""Tests for ONT provisioning state reconciliation.

Tests the state reconciliation system including:
- Desired/actual state building
- Delta computation with idempotency
- Validation (optical budget, VLAN trunk, ip_index bounds)
- Executor compensation and rollback
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# State Tests
# ---------------------------------------------------------------------------


class TestDesiredServicePort:
    """Test DesiredServicePort matching logic."""

    def test_exact_match_same_vlan_gem_transform(self) -> None:
        from app.services.network.ont_provisioning.state import (
            ActualServicePort,
            DesiredServicePort,
            ServicePortMatchResult,
        )

        desired = DesiredServicePort(
            vlan_id=100, gem_index=1, tag_transform="translate"
        )
        actual = ActualServicePort(
            index=5,
            vlan_id=100,
            gem_index=1,
            ont_id=3,
            state="up",
            tag_transform="translate",
        )
        assert desired.matches(actual) == ServicePortMatchResult.EXACT_MATCH

    def test_partial_match_different_tag_transform(self) -> None:
        from app.services.network.ont_provisioning.state import (
            ActualServicePort,
            DesiredServicePort,
            ServicePortMatchResult,
        )

        desired = DesiredServicePort(
            vlan_id=100, gem_index=1, tag_transform="translate"
        )
        actual = ActualServicePort(
            index=5,
            vlan_id=100,
            gem_index=1,
            ont_id=3,
            state="up",
            tag_transform="default",
        )
        assert desired.matches(actual) == ServicePortMatchResult.PARTIAL_MATCH

    def test_not_found_different_vlan(self) -> None:
        from app.services.network.ont_provisioning.state import (
            ActualServicePort,
            DesiredServicePort,
            ServicePortMatchResult,
        )

        desired = DesiredServicePort(vlan_id=100, gem_index=1)
        actual = ActualServicePort(
            index=5, vlan_id=200, gem_index=1, ont_id=3, state="up"
        )
        assert desired.matches(actual) == ServicePortMatchResult.NOT_FOUND

    def test_not_found_different_gem(self) -> None:
        from app.services.network.ont_provisioning.state import (
            ActualServicePort,
            DesiredServicePort,
            ServicePortMatchResult,
        )

        desired = DesiredServicePort(vlan_id=100, gem_index=1)
        actual = ActualServicePort(
            index=5, vlan_id=100, gem_index=2, ont_id=3, state="up"
        )
        assert desired.matches(actual) == ServicePortMatchResult.NOT_FOUND


class TestProvisioningDelta:
    """Test ProvisioningDelta properties."""

    def test_has_changes_with_create_action(self) -> None:
        from app.services.network.ont_provisioning.state import (
            DesiredServicePort,
            ProvisioningAction,
            ProvisioningDelta,
            ServicePortDelta,
        )

        delta = ProvisioningDelta(
            service_port_deltas=[
                ServicePortDelta(
                    action=ProvisioningAction.CREATE,
                    desired=DesiredServicePort(vlan_id=100, gem_index=1),
                    actual=None,
                )
            ]
        )
        assert delta.has_changes is True

    def test_no_changes_with_only_noops(self) -> None:
        from app.services.network.ont_provisioning.state import (
            ActualServicePort,
            DesiredServicePort,
            ProvisioningAction,
            ProvisioningDelta,
            ServicePortDelta,
        )

        delta = ProvisioningDelta(
            service_port_deltas=[
                ServicePortDelta(
                    action=ProvisioningAction.NOOP,
                    desired=DesiredServicePort(vlan_id=100, gem_index=1),
                    actual=ActualServicePort(
                        index=5, vlan_id=100, gem_index=1, ont_id=3, state="up"
                    ),
                )
            ]
        )
        assert delta.has_changes is False

    def test_is_valid_all_validations_pass(self) -> None:
        from app.services.network.ont_provisioning.state import ProvisioningDelta

        delta = ProvisioningDelta(
            optical_budget_ok=True,
            mgmt_vlan_trunked=True,
            ip_index_valid=True,
        )
        assert delta.is_valid is True

    def test_is_valid_false_when_optical_fails(self) -> None:
        from app.services.network.ont_provisioning.state import ProvisioningDelta

        delta = ProvisioningDelta(
            optical_budget_ok=False,
            optical_budget_message="Below sensitivity",
            mgmt_vlan_trunked=True,
            ip_index_valid=True,
        )
        assert delta.is_valid is False


# ---------------------------------------------------------------------------
# Reconciler Tests
# ---------------------------------------------------------------------------


class TestComputeDelta:
    """Test delta computation logic."""

    def test_existing_port_becomes_noop(self) -> None:
        from app.services.network.ont_provisioning.reconciler import compute_delta
        from app.services.network.ont_provisioning.state import (
            ActualOntState,
            ActualServicePort,
            DesiredOntState,
            DesiredServicePort,
            ProvisioningAction,
        )

        desired = DesiredOntState(
            ont_id="test-123",
            serial_number="TEST-001",
            fsp="0/2/1",
            olt_ont_id=5,
            service_ports=(DesiredServicePort(vlan_id=100, gem_index=1),),
        )
        actual = ActualOntState(
            is_authorized=True,
            olt_ont_id=5,
            service_ports=(
                ActualServicePort(
                    index=10, vlan_id=100, gem_index=1, ont_id=5, state="up"
                ),
            ),
        )

        delta = compute_delta(desired, actual)

        assert len(delta.service_port_deltas) == 1
        assert delta.service_port_deltas[0].action == ProvisioningAction.NOOP

    def test_missing_port_becomes_create(self) -> None:
        from app.services.network.ont_provisioning.reconciler import compute_delta
        from app.services.network.ont_provisioning.state import (
            ActualOntState,
            DesiredOntState,
            DesiredServicePort,
            ProvisioningAction,
        )

        desired = DesiredOntState(
            ont_id="test-123",
            serial_number="TEST-001",
            fsp="0/2/1",
            olt_ont_id=5,
            service_ports=(DesiredServicePort(vlan_id=100, gem_index=1),),
        )
        actual = ActualOntState(
            is_authorized=True,
            olt_ont_id=5,
            service_ports=(),  # No existing ports
        )

        delta = compute_delta(desired, actual)

        assert len(delta.service_port_deltas) == 1
        assert delta.service_port_deltas[0].action == ProvisioningAction.CREATE

    def test_multiple_ports_mixed_actions(self) -> None:
        from app.services.network.ont_provisioning.reconciler import compute_delta
        from app.services.network.ont_provisioning.state import (
            ActualOntState,
            ActualServicePort,
            DesiredOntState,
            DesiredServicePort,
            ProvisioningAction,
        )

        desired = DesiredOntState(
            ont_id="test-123",
            serial_number="TEST-001",
            fsp="0/2/1",
            olt_ont_id=5,
            service_ports=(
                DesiredServicePort(vlan_id=100, gem_index=1),  # Exists
                DesiredServicePort(vlan_id=200, gem_index=2),  # New
            ),
        )
        actual = ActualOntState(
            is_authorized=True,
            olt_ont_id=5,
            service_ports=(
                ActualServicePort(
                    index=10, vlan_id=100, gem_index=1, ont_id=5, state="up"
                ),
            ),
        )

        delta = compute_delta(desired, actual)

        actions = {d.action for d in delta.service_port_deltas}
        assert ProvisioningAction.NOOP in actions
        assert ProvisioningAction.CREATE in actions

    def test_management_config_triggers_needs_flag(self) -> None:
        from app.services.network.ont_provisioning.reconciler import compute_delta
        from app.services.network.ont_provisioning.state import (
            ActualOntState,
            DesiredManagementConfig,
            DesiredOntState,
        )

        desired = DesiredOntState(
            ont_id="test-123",
            serial_number="TEST-001",
            fsp="0/2/1",
            olt_ont_id=5,
            management=DesiredManagementConfig(vlan_tag=999, ip_mode="dhcp"),
        )
        actual = ActualOntState(
            is_authorized=True,
            olt_ont_id=5,
        )

        delta = compute_delta(desired, actual)

        assert delta.needs_mgmt_ip_config is True


# ---------------------------------------------------------------------------
# Optical Budget Tests
# ---------------------------------------------------------------------------


class TestOpticalBudgetValidation:
    """Test optical budget validation."""

    def test_no_reading_allows_provisioning(self) -> None:
        from app.services.network.ont_provisioning.optical_budget import (
            validate_optical_budget,
        )

        ont = SimpleNamespace(onu_rx_signal_dbm=None)
        result = validate_optical_budget(ont)
        assert result.is_valid is True
        assert result.rx_power_dbm is None

    def test_good_signal_is_valid(self) -> None:
        from app.services.network.ont_provisioning.optical_budget import (
            validate_optical_budget,
        )

        ont = SimpleNamespace(onu_rx_signal_dbm=-20.0)  # Good signal
        result = validate_optical_budget(ont)
        assert result.is_valid is True
        assert result.is_warning is False

    def test_below_sensitivity_is_invalid(self) -> None:
        from app.services.network.ont_provisioning.optical_budget import (
            validate_optical_budget,
        )

        ont = SimpleNamespace(onu_rx_signal_dbm=-30.0)  # Below -28 dBm
        result = validate_optical_budget(ont)
        assert result.is_valid is False
        assert "sensitivity" in result.message.lower()

    def test_overload_is_invalid(self) -> None:
        from app.services.network.ont_provisioning.optical_budget import (
            validate_optical_budget,
        )

        ont = SimpleNamespace(onu_rx_signal_dbm=-5.0)  # Above -8 dBm
        result = validate_optical_budget(ont)
        assert result.is_valid is False
        assert "overload" in result.message.lower()

    def test_low_margin_is_warning(self) -> None:
        from app.services.network.ont_provisioning.optical_budget import (
            validate_optical_budget,
        )

        ont = SimpleNamespace(onu_rx_signal_dbm=-26.5)  # Only 1.5 dB margin
        result = validate_optical_budget(ont)
        assert result.is_valid is True
        assert result.is_warning is True


# ---------------------------------------------------------------------------
# VLAN Validator Tests
# ---------------------------------------------------------------------------


class TestVlanValidator:
    """Test VLAN validation."""

    def test_vlan_exists_returns_valid(self, db_session) -> None:
        from app.models.catalog import RegionZone
        from app.models.network import OLTDevice, Vlan
        from app.services.network.ont_provisioning.vlan_validator import (
            validate_vlan_exists,
        )

        # Create OLT and VLAN
        olt = OLTDevice(name="Test OLT", vendor="Huawei", model="MA5608T")
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        region = RegionZone(name="Test Region")
        db_session.add(region)
        db_session.commit()

        vlan = Vlan(tag=100, region_id=region.id, olt_device_id=olt.id, is_active=True)
        db_session.add(vlan)
        db_session.commit()

        result = validate_vlan_exists(db_session, 100, olt)
        assert result.is_valid is True
        assert result.exists_in_db is True

    def test_vlan_not_found_returns_invalid(self, db_session) -> None:
        from app.models.network import OLTDevice
        from app.services.network.ont_provisioning.vlan_validator import (
            validate_vlan_exists,
        )

        olt = OLTDevice(name="Test OLT", vendor="Huawei", model="MA5608T")
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        result = validate_vlan_exists(db_session, 999, olt)
        assert result.is_valid is False
        assert result.exists_in_db is False

    def test_global_vlan_is_invalid_for_scoped_olt(self, db_session) -> None:
        from app.models.catalog import RegionZone
        from app.models.network import OLTDevice, Vlan
        from app.services.network.ont_provisioning.vlan_validator import (
            validate_vlan_exists,
        )

        olt = OLTDevice(name="Test OLT", vendor="Huawei", model="MA5608T")
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        region = RegionZone(name="Test Region 2")
        db_session.add(region)
        db_session.commit()

        vlan = Vlan(tag=200, region_id=region.id, is_active=True)
        db_session.add(vlan)
        db_session.commit()

        result = validate_vlan_exists(db_session, 200, olt)
        assert result.is_valid is False
        assert result.exists_in_db is False


# ---------------------------------------------------------------------------
# SSH Session Tests
# ---------------------------------------------------------------------------


class TestErrorCodeParsing:
    """Test structured error code detection."""

    def test_already_exists_is_idempotent_success(self) -> None:
        from app.services.network.olt_ssh_session import (
            ErrorCode,
            parse_command_result,
        )

        output = "Error: service virtual port has existed already"
        result = parse_command_result(output)
        assert result.success is True  # Idempotent success
        assert result.error_code == ErrorCode.ALREADY_EXISTS

    def test_vlan_not_exist_is_failure(self) -> None:
        from app.services.network.olt_ssh_session import (
            ErrorCode,
            parse_command_result,
        )

        output = "Error: vlan 999 does not exist"
        result = parse_command_result(output)
        assert result.success is False
        assert result.error_code == ErrorCode.VLAN_NOT_EXIST

    def test_success_with_no_error_patterns(self) -> None:
        from app.services.network.olt_ssh_session import (
            ErrorCode,
            parse_command_result,
        )

        output = "Service-port 10 created successfully"
        result = parse_command_result(output)
        assert result.success is True
        assert result.error_code == ErrorCode.NONE

    def test_parameter_error_detected(self) -> None:
        from app.services.network.olt_ssh_session import (
            ErrorCode,
            parse_command_result,
        )

        output = "% parameter error, the input parameter is invalid"
        result = parse_command_result(output)
        assert result.success is False
        assert result.error_code == ErrorCode.PARAMETER_ERROR


# ---------------------------------------------------------------------------
# Executor Tests
# ---------------------------------------------------------------------------


class TestProvisioningExecutionResult:
    """Test execution result and rollback."""

    def test_rollback_executes_compensation_in_reverse(self) -> None:
        from app.services.network.ont_provisioning.executor import (
            CompensationEntry,
            ProvisioningExecutionResult,
        )

        result = ProvisioningExecutionResult(
            success=False,
            message="Test failure",
            compensation_log=[
                CompensationEntry(
                    step_name="step1",
                    undo_commands=["undo step1"],
                    description="Undo step 1",
                ),
                CompensationEntry(
                    step_name="step2",
                    undo_commands=["undo step2"],
                    description="Undo step 2",
                ),
            ],
        )

        # Mock OLT to avoid real SSH connection
        mock_olt = MagicMock()

        with patch(
            "app.services.network.ont_provisioning.executor.olt_session"
        ) as mock_session:
            # Setup mock session context
            mock_sess = MagicMock()
            mock_sess.run_config_command.return_value = MagicMock(
                success=True, is_idempotent_success=False, message="OK"
            )
            mock_session.return_value.__enter__.return_value = mock_sess

            rollback_results = result.rollback(mock_olt)

            # Should have 2 results (one for each compensation)
            assert len(rollback_results) == 2
            # Should be in reverse order
            assert rollback_results[0][0] == "step2"
            assert rollback_results[1][0] == "step1"


# ---------------------------------------------------------------------------
# Integration-Style Tests
# ---------------------------------------------------------------------------


class TestBuildDesiredStateFromProfile:
    """Test building desired state from profile."""

    def test_builds_service_ports_from_wan_services(self, db_session) -> None:
        """Test that service ports come from profile WAN services, not reference ONT."""
        from app.models.network import (
            OLTDevice,
            OntAssignment,
            OntBundleAssignment,
            OntBundleAssignmentStatus,
            OntProfileWanService,
            OntProvisioningProfile,
            OntUnit,
            PonPort,
            VlanMode,
            WanServiceType,
        )
        from app.services.network.ont_provisioning.state import (
            build_desired_state_from_profile,
        )

        # Create OLT
        olt = OLTDevice(
            name="Test OLT",
            vendor="Huawei",
            model="MA5608T",
            ssh_username="admin",
            ssh_password="test",
        )
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        # Create PON port
        pon = PonPort(olt_id=olt.id, name="0/2/1")
        db_session.add(pon)
        db_session.commit()
        db_session.refresh(pon)

        # Create profile with WAN services
        profile = OntProvisioningProfile(
            name="Test Profile",
            olt_device_id=olt.id,
        )
        db_session.add(profile)
        db_session.commit()
        db_session.refresh(profile)

        # Add WAN service to profile
        wan_service = OntProfileWanService(
            profile_id=profile.id,
            service_type=WanServiceType.internet,
            vlan_mode=VlanMode.tagged,
            s_vlan=100,
            gem_port_id=1,
        )
        db_session.add(wan_service)
        db_session.commit()

        # Create ONT
        ont = OntUnit(
            serial_number="TEST-PROFILE-001",
            board="0/2",
            port="1",
            external_id="5",
        )
        db_session.add(ont)
        db_session.flush()

        # Create assignment
        assignment = OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        db_session.add(assignment)
        db_session.add(
            OntBundleAssignment(
                ont_unit_id=ont.id,
                bundle_id=profile.id,
                status=OntBundleAssignmentStatus.applied,
                is_active=True,
            )
        )
        db_session.commit()
        db_session.refresh(ont)

        # Build desired state
        desired, err = build_desired_state_from_profile(
            db_session, str(ont.id), profile
        )

        assert err == ""
        assert desired is not None
        assert len(desired.service_ports) == 1
        assert desired.service_ports[0].vlan_id == 100
        assert desired.service_ports[0].gem_index == 1

    def test_build_desired_state_uses_effective_management_ip(self, db_session) -> None:
        from app.models.network import (
            MgmtIpMode,
            OLTDevice,
            OntAssignment,
            OntBundleAssignment,
            OntBundleAssignmentStatus,
            OntConfigOverride,
            OntProvisioningProfile,
            OntUnit,
            PonPort,
        )
        from app.services.network.ont_provisioning.state import (
            build_desired_state_from_profile,
        )

        olt = OLTDevice(
            name="State OLT",
            vendor="Huawei",
            model="MA5608T",
            ssh_username="admin",
            ssh_password="test",
        )
        db_session.add(olt)
        db_session.flush()

        pon = PonPort(olt_id=olt.id, name="0/2/1")
        db_session.add(pon)
        db_session.flush()

        legacy_bundle = OntProvisioningProfile(name="Legacy State Bundle", is_active=True)
        profile = OntProvisioningProfile(
            name="Target State Bundle",
            olt_device_id=olt.id,
            mgmt_ip_mode=MgmtIpMode.static_ip,
            mgmt_vlan_tag=300,
        )
        db_session.add_all([legacy_bundle, profile])
        db_session.flush()

        ont = OntUnit(
            serial_number="TEST-STATE-EFFECTIVE-001",
            board="0/2",
            port="1",
            external_id="5",
            mgmt_ip_address=None,
        )
        db_session.add(ont)
        db_session.flush()

        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        )
        db_session.add(
            OntBundleAssignment(
                ont_unit_id=ont.id,
                bundle_id=legacy_bundle.id,
                status=OntBundleAssignmentStatus.applied,
                is_active=True,
            )
        )
        db_session.add(
            OntConfigOverride(
                ont_unit_id=ont.id,
                field_name="management.ip_address",
                value_json={"value": "10.30.0.44"},
            )
        )
        db_session.commit()

        desired, err = build_desired_state_from_profile(
            db_session, str(ont.id), profile
        )

        assert err == ""
        assert desired is not None
        assert desired.management is not None
        assert desired.management.ip_address == "10.30.0.44"


# ---------------------------------------------------------------------------
# Preflight Tests
# ---------------------------------------------------------------------------


class TestProvisioningPreflight:
    """Test provisioning readiness gates before device actions are allowed."""

    def _create_preflight_ont(self, db_session, *, authorized: bool = False):
        from app.models.network import (
            OLTDevice,
            OntAssignment,
            OntAuthorizationStatus,
            OntBundleAssignment,
            OntBundleAssignmentStatus,
            OntProvisioningProfile,
            OntUnit,
            PonPort,
        )

        olt = OLTDevice(
            name="Preflight OLT",
            vendor="Huawei",
            model="MA5608T",
            ssh_username="admin",
            ssh_password="secret",
        )
        db_session.add(olt)
        db_session.flush()

        pon = PonPort(olt_id=olt.id, name="0/2/1")
        profile = OntProvisioningProfile(
            name="Preflight Profile",
            olt_device_id=olt.id,
            authorization_line_profile_id=10,
            authorization_service_profile_id=20,
        )
        db_session.add_all([pon, profile])
        db_session.flush()

        ont = OntUnit(
            serial_number="PRE-FLIGHT-001",
            olt_device_id=olt.id,
            board="0/2",
            port="1",
            external_id="5",
            authorization_status=(
                OntAuthorizationStatus.authorized if authorized else None
            ),
        )
        db_session.add(ont)
        db_session.flush()

        assignment = OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            active=True,
        )
        db_session.add(assignment)
        db_session.add(
            OntBundleAssignment(
                ont_unit_id=ont.id,
                bundle_id=profile.id,
                status=OntBundleAssignmentStatus.applied,
                is_active=True,
            )
        )
        db_session.commit()
        return ont

    def test_preflight_blocks_before_olt_authorization(self, db_session) -> None:
        from app.services.network.ont_provisioning.preflight import (
            validate_prerequisites,
        )

        ont = self._create_preflight_ont(db_session, authorized=False)

        result = validate_prerequisites(db_session, str(ont.id))
        checks = {check["name"]: check for check in result["checks"]}

        assert result["ready"] is False
        assert result["ready_to_authorize"] is True
        assert result["ready_to_provision"] is False
        assert checks["OLT authorization"]["status"] == "fail"
        assert "Authorize the ONT" in checks["OLT authorization"]["message"]
        assert checks["OLT authorization"]["blocks_authorization"] is False

    def test_preflight_allows_authorized_ont_with_required_inventory(
        self, db_session
    ) -> None:
        from app.services.network.ont_provisioning.preflight import (
            validate_prerequisites,
        )

        ont = self._create_preflight_ont(db_session, authorized=True)

        result = validate_prerequisites(db_session, str(ont.id))
        checks = {check["name"]: check for check in result["checks"]}

        assert result["ready"] is True
        assert result["ready_to_authorize"] is True
        assert result["ready_to_provision"] is True
        assert checks["OLT authorization"]["status"] == "ok"
        assert checks["OLT ONT-ID"]["status"] == "ok"
        assert checks["Active PON assignment"]["status"] == "ok"
        assert checks["Authorization profiles"]["status"] == "ok"

    def test_preflight_requires_pon_assignment(self, db_session) -> None:
        from app.models.network import OntAssignment
        from app.services.network.ont_provisioning.preflight import (
            validate_prerequisites,
        )

        ont = self._create_preflight_ont(db_session, authorized=True)
        for assignment in db_session.query(OntAssignment).all():
            assignment.active = False
        db_session.commit()

        result = validate_prerequisites(db_session, str(ont.id))
        checks = {check["name"]: check for check in result["checks"]}

        assert result["ready"] is False
        assert result["ready_to_authorize"] is False
        assert checks["Active PON assignment"]["status"] == "fail"

    def test_effective_tr069_profile_returns_first_profile(self, monkeypatch) -> None:
        from types import SimpleNamespace

        from app.services import web_network_onts

        expected = SimpleNamespace(profile_id=7, name="ACS")
        monkeypatch.setattr(
            web_network_onts,
            "get_tr069_profiles_for_ont",
            lambda db, ont: ([expected], None),
        )

        profile, error = web_network_onts.resolve_effective_tr069_profile_for_ont(
            object(),
            object(),
        )

        assert error is None
        assert profile is expected
