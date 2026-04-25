"""Tests for batched OLT management setup."""

from unittest.mock import MagicMock, patch

import pytest

from app.services.network.olt_batched_mgmt import (
    BatchedMgmtResult,
    BatchedMgmtSpec,
    build_management_command_batch,
    create_batched_mgmt_spec_from_config_pack,
    execute_batched_management_setup,
)


class TestBatchedMgmtSpec:
    """Tests for BatchedMgmtSpec dataclass."""

    def test_minimal_spec(self):
        """Create spec with minimal required fields."""
        spec = BatchedMgmtSpec(fsp="0/1/0", ont_id_on_olt=5)

        assert spec.fsp == "0/1/0"
        assert spec.ont_id_on_olt == 5
        assert spec.mgmt_vlan_tag is None
        assert spec.mgmt_gem_index == 2
        assert spec.ip_mode == "dhcp"

    def test_full_spec(self):
        """Create spec with all fields populated."""
        spec = BatchedMgmtSpec(
            fsp="0/2/1",
            ont_id_on_olt=10,
            mgmt_vlan_tag=201,
            mgmt_gem_index=3,
            ip_mode="static",
            ip_address="10.0.0.100",
            subnet_mask="255.255.255.0",
            gateway="10.0.0.1",
            ip_priority=5,
            ip_index=0,
            internet_config_ip_index=0,
            wan_config_profile_id=1,
            tr069_profile_id=2,
        )

        assert spec.mgmt_vlan_tag == 201
        assert spec.ip_mode == "static"
        assert spec.ip_address == "10.0.0.100"
        assert spec.tr069_profile_id == 2

    def test_has_service_port(self):
        """has_service_port depends on mgmt_vlan_tag."""
        spec = BatchedMgmtSpec(fsp="0/1/0", ont_id_on_olt=1)
        assert spec.has_service_port is False

        spec = BatchedMgmtSpec(fsp="0/1/0", ont_id_on_olt=1, mgmt_vlan_tag=201)
        assert spec.has_service_port is True

    def test_has_iphost(self):
        """has_iphost depends on mgmt_vlan_tag."""
        spec = BatchedMgmtSpec(fsp="0/1/0", ont_id_on_olt=1)
        assert spec.has_iphost is False

        spec = BatchedMgmtSpec(fsp="0/1/0", ont_id_on_olt=1, mgmt_vlan_tag=201)
        assert spec.has_iphost is True

    def test_has_internet_config(self):
        """has_internet_config depends on internet_config_ip_index."""
        spec = BatchedMgmtSpec(fsp="0/1/0", ont_id_on_olt=1)
        assert spec.has_internet_config is False

        spec = BatchedMgmtSpec(fsp="0/1/0", ont_id_on_olt=1, internet_config_ip_index=0)
        assert spec.has_internet_config is True

    def test_has_wan_config(self):
        """has_wan_config depends on wan_config_profile_id."""
        spec = BatchedMgmtSpec(fsp="0/1/0", ont_id_on_olt=1)
        assert spec.has_wan_config is False

        spec = BatchedMgmtSpec(fsp="0/1/0", ont_id_on_olt=1, wan_config_profile_id=1)
        assert spec.has_wan_config is True

    def test_has_tr069(self):
        """has_tr069 depends on tr069_profile_id."""
        spec = BatchedMgmtSpec(fsp="0/1/0", ont_id_on_olt=1)
        assert spec.has_tr069 is False

        spec = BatchedMgmtSpec(fsp="0/1/0", ont_id_on_olt=1, tr069_profile_id=1)
        assert spec.has_tr069 is True


class TestBatchedMgmtResult:
    """Tests for BatchedMgmtResult dataclass."""

    def test_successful_result(self):
        """Create successful result."""
        result = BatchedMgmtResult(
            success=True,
            steps_completed=["configure_iphost", "bind_tr069"],
        )

        assert result.success is True
        assert len(result.steps_completed) == 2
        assert "Management setup complete" in result.message

    def test_failed_result(self):
        """Create failed result."""
        result = BatchedMgmtResult(
            success=False,
            steps_completed=["configure_iphost"],
            steps_failed=["bind_tr069"],
            error_message="TR-069 profile not found",
        )

        assert result.success is False
        assert len(result.steps_failed) == 1
        assert result.message == "TR-069 profile not found"

    def test_message_default(self):
        """Message should have default for failed result."""
        result = BatchedMgmtResult(success=False)
        assert result.message == "Management setup failed"


class TestBuildManagementCommandBatch:
    """Tests for build_management_command_batch function."""

    def test_empty_spec(self):
        """Empty spec should yield no commands."""
        spec = BatchedMgmtSpec(fsp="0/1/0", ont_id_on_olt=1)
        commands = build_management_command_batch(spec)

        assert commands == []

    def test_service_port_only(self):
        """Spec with only service-port should generate correct commands."""
        spec = BatchedMgmtSpec(
            fsp="0/1/0",
            ont_id_on_olt=5,
            mgmt_vlan_tag=201,
            mgmt_gem_index=2,
        )
        commands = build_management_command_batch(spec)

        # Should have: service-port, enter interface, iphost, quit
        assert len(commands) == 4

        # Check service-port command
        sp_cmd, sp_desc = commands[0]
        assert sp_desc == "create_mgmt_service_port"
        assert "service-port vlan 201" in sp_cmd
        assert "gpon 0/1/0 ont 5" in sp_cmd
        assert "gemport 2" in sp_cmd

    def test_static_ip_mode(self):
        """Static IP mode should generate correct iphost command."""
        spec = BatchedMgmtSpec(
            fsp="0/2/1",
            ont_id_on_olt=10,
            mgmt_vlan_tag=201,
            ip_mode="static",
            ip_address="10.0.0.100",
            subnet_mask="255.255.255.0",
            gateway="10.0.0.1",
        )
        commands = build_management_command_batch(spec)

        # Find iphost command
        iphost_cmds = [c for c, d in commands if d == "configure_iphost"]
        assert len(iphost_cmds) == 1

        iphost_cmd = iphost_cmds[0]
        assert "static" in iphost_cmd
        assert "ip-address 10.0.0.100" in iphost_cmd
        assert "mask 255.255.255.0" in iphost_cmd
        assert "gateway 10.0.0.1" in iphost_cmd

    def test_dhcp_mode(self):
        """DHCP mode should generate simpler iphost command."""
        spec = BatchedMgmtSpec(
            fsp="0/2/1",
            ont_id_on_olt=10,
            mgmt_vlan_tag=201,
            ip_mode="dhcp",
        )
        commands = build_management_command_batch(spec)

        # Find iphost command
        iphost_cmds = [c for c, d in commands if d == "configure_iphost"]
        assert len(iphost_cmds) == 1

        iphost_cmd = iphost_cmds[0]
        assert "dhcp" in iphost_cmd
        assert "ip-address" not in iphost_cmd

    def test_full_config(self):
        """Full config should generate all commands."""
        spec = BatchedMgmtSpec(
            fsp="0/1/0",
            ont_id_on_olt=5,
            mgmt_vlan_tag=201,
            internet_config_ip_index=0,
            wan_config_profile_id=1,
            tr069_profile_id=2,
        )
        commands = build_management_command_batch(spec)

        descriptions = [d for _, d in commands]

        assert "create_mgmt_service_port" in descriptions
        assert "enter_interface_mode" in descriptions
        assert "configure_iphost" in descriptions
        assert "activate_internet_config" in descriptions
        assert "configure_wan" in descriptions
        assert "bind_tr069" in descriptions
        assert "exit_interface_mode" in descriptions

    def test_interface_mode_wrapping(self):
        """Interface commands should be wrapped with enter/exit."""
        spec = BatchedMgmtSpec(
            fsp="0/3/2",
            ont_id_on_olt=15,
            mgmt_vlan_tag=201,
            tr069_profile_id=1,
        )
        commands = build_management_command_batch(spec)

        # Find interface enter command
        enter_cmds = [(i, c) for i, (c, d) in enumerate(commands) if d == "enter_interface_mode"]
        assert len(enter_cmds) == 1

        idx, cmd = enter_cmds[0]
        assert "interface gpon 0/3" in cmd

        # Exit should be after
        exit_cmds = [(i, c) for i, (c, d) in enumerate(commands) if d == "exit_interface_mode"]
        assert len(exit_cmds) == 1
        exit_idx, _ = exit_cmds[0]
        assert exit_idx > idx

    def test_wan_config_requires_internet_config(self):
        """wan-config should only be generated if internet_config_ip_index is set."""
        # Without internet_config_ip_index
        spec1 = BatchedMgmtSpec(
            fsp="0/1/0",
            ont_id_on_olt=5,
            wan_config_profile_id=1,  # Set, but no internet_config_ip_index
        )
        commands1 = build_management_command_batch(spec1)
        wan_cmds1 = [d for _, d in commands1 if d == "configure_wan"]
        assert len(wan_cmds1) == 0

        # With internet_config_ip_index
        spec2 = BatchedMgmtSpec(
            fsp="0/1/0",
            ont_id_on_olt=5,
            mgmt_vlan_tag=201,
            internet_config_ip_index=0,
            wan_config_profile_id=1,
        )
        commands2 = build_management_command_batch(spec2)
        wan_cmds2 = [d for _, d in commands2 if d == "configure_wan"]
        assert len(wan_cmds2) == 1


class TestExecuteBatchedManagementSetup:
    """Tests for execute_batched_management_setup function."""

    def test_empty_spec_succeeds(self):
        """Empty spec should succeed with no commands."""
        spec = BatchedMgmtSpec(fsp="0/1/0", ont_id_on_olt=1)
        olt = MagicMock()

        result = execute_batched_management_setup(olt, spec)

        assert result.success is True
        assert "No management configuration specified" in result.error_message

    @patch("app.services.network.olt_ssh_session.olt_session")
    def test_successful_execution(self, mock_session_ctx):
        """Commands should execute successfully."""
        spec = BatchedMgmtSpec(
            fsp="0/1/0",
            ont_id_on_olt=5,
            mgmt_vlan_tag=201,
            tr069_profile_id=1,
        )
        olt = MagicMock()
        olt.name = "Test-OLT"

        # Mock SSH session
        mock_session = MagicMock()
        mock_cmd_result = MagicMock()
        mock_cmd_result.success = True
        mock_cmd_result.output = "Success"
        mock_session.run_command.return_value = mock_cmd_result
        mock_session_ctx.return_value.__enter__.return_value = mock_session

        result = execute_batched_management_setup(olt, spec)

        assert result.success is True
        assert len(result.steps_completed) > 0
        assert len(result.steps_failed) == 0

    @patch("app.services.network.olt_ssh_session.olt_session")
    def test_idempotent_error_handled(self, mock_session_ctx):
        """Idempotent errors should be treated as success."""
        spec = BatchedMgmtSpec(
            fsp="0/1/0",
            ont_id_on_olt=5,
            mgmt_vlan_tag=201,
        )
        olt = MagicMock()
        olt.name = "Test-OLT"

        mock_session = MagicMock()
        mock_cmd_result = MagicMock()
        mock_cmd_result.success = False
        mock_cmd_result.output = "Service-port already exists"
        mock_session.run_command.return_value = mock_cmd_result
        mock_session_ctx.return_value.__enter__.return_value = mock_session

        result = execute_batched_management_setup(olt, spec)

        # Should still succeed due to idempotent handling
        assert result.success is True
        # Step should be marked as exists
        assert any("exists" in step for step in result.steps_completed)

    @patch("app.services.network.olt_ssh_session.olt_session")
    def test_non_critical_failure_succeeds(self, mock_session_ctx):
        """Non-critical step failures should not cause overall failure."""
        spec = BatchedMgmtSpec(
            fsp="0/1/0",
            ont_id_on_olt=5,
            mgmt_vlan_tag=201,
            internet_config_ip_index=0,  # Non-critical
        )
        olt = MagicMock()
        olt.name = "Test-OLT"

        mock_session = MagicMock()

        def mock_run_command(cmd, require_mode=None):
            result = MagicMock()
            if "internet-config" in cmd:
                result.success = False
                result.output = "ONT does not support this feature"
            else:
                result.success = True
                result.output = "Success"
            return result

        mock_session.run_command.side_effect = mock_run_command
        mock_session_ctx.return_value.__enter__.return_value = mock_session

        result = execute_batched_management_setup(olt, spec)

        # Should still succeed because internet-config is non-critical
        assert result.success is True
        assert "activate_internet_config" in result.steps_failed

    @patch("app.services.network.olt_ssh_session.olt_session")
    def test_critical_failure_fails(self, mock_session_ctx):
        """Critical step failures should cause overall failure."""
        spec = BatchedMgmtSpec(
            fsp="0/1/0",
            ont_id_on_olt=5,
            mgmt_vlan_tag=201,  # iphost is critical
        )
        olt = MagicMock()
        olt.name = "Test-OLT"

        mock_session = MagicMock()

        def mock_run_command(cmd, require_mode=None):
            result = MagicMock()
            if "ipconfig" in cmd:
                result.success = False
                result.output = "Invalid parameters"
            else:
                result.success = True
                result.output = "Success"
            return result

        mock_session.run_command.side_effect = mock_run_command
        mock_session_ctx.return_value.__enter__.return_value = mock_session

        result = execute_batched_management_setup(olt, spec)

        assert result.success is False
        assert "configure_iphost" in result.steps_failed

    @patch("app.services.network.olt_ssh_session.olt_session")
    def test_exception_handling(self, mock_session_ctx):
        """Exceptions should be handled gracefully."""
        spec = BatchedMgmtSpec(
            fsp="0/1/0",
            ont_id_on_olt=5,
            mgmt_vlan_tag=201,
        )
        olt = MagicMock()
        olt.name = "Test-OLT"

        mock_session_ctx.return_value.__enter__.side_effect = Exception("Connection failed")

        result = execute_batched_management_setup(olt, spec)

        assert result.success is False
        assert "Connection failed" in result.error_message


class TestCreateBatchedMgmtSpecFromConfigPack:
    """Tests for create_batched_mgmt_spec_from_config_pack function."""

    def test_dhcp_mode(self):
        """Should create DHCP spec when no static IP provided."""
        config_pack = MagicMock()
        config_pack.management_vlan.tag = 201
        config_pack.mgmt_gem_index = 2
        config_pack.internet_config_ip_index = 0
        config_pack.wan_config_profile_id = 1
        config_pack.tr069_olt_profile_id = 5

        spec = create_batched_mgmt_spec_from_config_pack(
            config_pack,
            fsp="0/1/0",
            ont_id_on_olt=10,
        )

        assert spec.fsp == "0/1/0"
        assert spec.ont_id_on_olt == 10
        assert spec.mgmt_vlan_tag == 201
        assert spec.ip_mode == "dhcp"
        assert spec.ip_address is None

    def test_static_mode(self):
        """Should create static spec when IP provided."""
        config_pack = MagicMock()
        config_pack.management_vlan.tag = 201
        config_pack.mgmt_gem_index = 2
        config_pack.internet_config_ip_index = 0
        config_pack.wan_config_profile_id = 1
        config_pack.tr069_olt_profile_id = 5

        spec = create_batched_mgmt_spec_from_config_pack(
            config_pack,
            fsp="0/1/0",
            ont_id_on_olt=10,
            allocated_ip="10.0.0.100",
            subnet_mask="255.255.255.0",
            gateway="10.0.0.1",
        )

        assert spec.ip_mode == "static"
        assert spec.ip_address == "10.0.0.100"
        assert spec.subnet_mask == "255.255.255.0"
        assert spec.gateway == "10.0.0.1"

    def test_partial_static_falls_back_to_dhcp(self):
        """Should use DHCP if not all static params provided."""
        config_pack = MagicMock()
        config_pack.management_vlan.tag = 201
        config_pack.mgmt_gem_index = 2
        config_pack.internet_config_ip_index = None
        config_pack.wan_config_profile_id = None
        config_pack.tr069_olt_profile_id = None

        spec = create_batched_mgmt_spec_from_config_pack(
            config_pack,
            fsp="0/1/0",
            ont_id_on_olt=10,
            allocated_ip="10.0.0.100",  # Only IP, no mask/gateway
        )

        assert spec.ip_mode == "dhcp"

    def test_null_wan_config_profile(self):
        """Should handle 0 wan_config_profile_id."""
        config_pack = MagicMock()
        config_pack.management_vlan.tag = 201
        config_pack.mgmt_gem_index = 2
        config_pack.internet_config_ip_index = 0
        config_pack.wan_config_profile_id = 0
        config_pack.tr069_olt_profile_id = 5

        spec = create_batched_mgmt_spec_from_config_pack(
            config_pack,
            fsp="0/1/0",
            ont_id_on_olt=10,
        )

        assert spec.wan_config_profile_id is None
