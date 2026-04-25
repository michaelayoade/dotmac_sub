"""Batched OLT management setup for improved post-authorization performance.

This module provides single-session execution of all management configuration
commands after ONT authorization, reducing SSH overhead from 5 sessions to 1.

Commands executed in batch:
1. Create management service-port (GEM index + VLAN)
2. Configure IPHOST (DHCP or static IP)
3. Activate internet-config (TCP stack)
4. Configure wan-config (route+NAT mode)
5. Bind TR-069 profile

Target: Reduce management setup from ~25s to ~5s (5x faster).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.network import OLTDevice

logger = logging.getLogger(__name__)


@dataclass
class BatchedMgmtSpec:
    """Specification for batched management configuration.

    All fields are optional except fsp and ont_id_on_olt.
    Only provided configurations will be executed.
    """

    fsp: str  # Frame/Slot/Port (e.g., "0/1/0")
    ont_id_on_olt: int

    # Management service-port configuration
    mgmt_vlan_tag: int | None = None
    mgmt_gem_index: int = 2  # Standard GEM for management

    # IPHOST configuration
    ip_mode: str = "dhcp"  # "dhcp" or "static"
    ip_address: str | None = None
    subnet_mask: str | None = None
    gateway: str | None = None
    ip_priority: int = 0
    ip_index: int = 0

    # internet-config (TCP stack activation)
    internet_config_ip_index: int | None = None

    # wan-config (route+NAT mode)
    wan_config_profile_id: int | None = None

    # TR-069 profile binding
    tr069_profile_id: int | None = None

    @property
    def has_service_port(self) -> bool:
        """True if service-port configuration is specified."""
        return self.mgmt_vlan_tag is not None

    @property
    def has_iphost(self) -> bool:
        """True if IPHOST configuration is specified."""
        return self.mgmt_vlan_tag is not None

    @property
    def has_internet_config(self) -> bool:
        """True if internet-config is specified."""
        return self.internet_config_ip_index is not None

    @property
    def has_wan_config(self) -> bool:
        """True if wan-config is specified."""
        return self.wan_config_profile_id is not None

    @property
    def has_tr069(self) -> bool:
        """True if TR-069 profile is specified."""
        return self.tr069_profile_id is not None


@dataclass
class BatchedMgmtResult:
    """Result of batched management configuration."""

    success: bool
    steps_completed: list[str] = field(default_factory=list)
    steps_failed: list[str] = field(default_factory=list)
    error_message: str | None = None
    details: dict[str, object] = field(default_factory=dict)
    raw_output: dict[str, str] = field(default_factory=dict)

    @property
    def message(self) -> str:
        """Human-readable result message."""
        if self.success:
            return f"Management setup complete ({len(self.steps_completed)} steps)"
        return self.error_message or "Management setup failed"


def build_management_command_batch(
    spec: BatchedMgmtSpec,
) -> list[tuple[str, str]]:
    """Build command sequence for single-session management setup.

    Returns list of tuples: (command, description)

    Command ordering:
    1. Global config: service-port create
    2. Interface mode: ont ipconfig, ont internet-config, ont wan-config, ont tr069-server-config
    """
    commands: list[tuple[str, str]] = []
    frame, slot, port = spec.fsp.split("/")

    # Phase 1: Service-port (global config mode)
    if spec.has_service_port:
        sp_cmd = (
            f"service-port vlan {spec.mgmt_vlan_tag} "
            f"gpon {spec.fsp} ont {spec.ont_id_on_olt} "
            f"gemport {spec.mgmt_gem_index} "
            f"multi-service user-vlan {spec.mgmt_vlan_tag} "
            f"tag-transform translate"
        )
        commands.append((sp_cmd, "create_mgmt_service_port"))

    # Phase 2: ONT configuration (interface mode)
    interface_commands: list[tuple[str, str]] = []

    # IPHOST configuration
    if spec.has_iphost:
        if spec.ip_mode == "static" and spec.ip_address and spec.subnet_mask and spec.gateway:
            iphost_cmd = (
                f"ont ipconfig {port} {spec.ont_id_on_olt} "
                f"ip-index {spec.ip_index} static "
                f"ip-address {spec.ip_address} "
                f"mask {spec.subnet_mask} "
                f"gateway {spec.gateway} "
                f"vlan {spec.mgmt_vlan_tag} "
                f"priority {spec.ip_priority}"
            )
        else:
            iphost_cmd = (
                f"ont ipconfig {port} {spec.ont_id_on_olt} "
                f"ip-index {spec.ip_index} dhcp "
                f"vlan {spec.mgmt_vlan_tag}"
            )
        interface_commands.append((iphost_cmd, "configure_iphost"))

    # internet-config (TCP stack activation)
    if spec.has_internet_config:
        inet_cmd = (
            f"ont internet-config {port} {spec.ont_id_on_olt} "
            f"ip-index {spec.internet_config_ip_index}"
        )
        interface_commands.append((inet_cmd, "activate_internet_config"))

    # wan-config (route+NAT mode)
    if spec.has_wan_config and spec.internet_config_ip_index is not None:
        wan_cmd = (
            f"ont wan-config {port} {spec.ont_id_on_olt} "
            f"ip-index {spec.internet_config_ip_index} "
            f"profile-id {spec.wan_config_profile_id}"
        )
        interface_commands.append((wan_cmd, "configure_wan"))

    # TR-069 profile binding
    if spec.has_tr069:
        tr069_cmd = (
            f"ont tr069-server-config {port} {spec.ont_id_on_olt} "
            f"profile-id {spec.tr069_profile_id}"
        )
        interface_commands.append((tr069_cmd, "bind_tr069"))

    # Wrap interface commands with enter/exit
    if interface_commands:
        commands.append(
            (f"interface gpon {frame}/{slot}", "enter_interface_mode")
        )
        commands.extend(interface_commands)
        commands.append(("quit", "exit_interface_mode"))

    return commands


def execute_batched_management_setup(
    olt: OLTDevice,
    spec: BatchedMgmtSpec,
) -> BatchedMgmtResult:
    """Execute all management commands in ONE SSH session.

    This function handles:
    - Single SSH connection for entire flow
    - Config mode entry/exit
    - Interface mode transitions
    - Error handling with partial completion tracking

    Args:
        olt: OLT device object
        spec: Batched management specification

    Returns:
        BatchedMgmtResult with success status and step tracking
    """
    from app.services.network.olt_ssh_session import CliMode, olt_session

    result = BatchedMgmtResult(success=False)
    commands = build_management_command_batch(spec)

    if not commands:
        result.success = True
        result.error_message = "No management configuration specified"
        return result

    logger.info(
        "Starting batched management setup for ONT %d on %s %s (%d commands)",
        spec.ont_id_on_olt,
        olt.name,
        spec.fsp,
        len(commands),
    )

    try:
        with olt_session(olt) as session:
            for cmd, description in commands:
                # Skip navigation commands in step tracking
                is_navigation = description in ("enter_interface_mode", "exit_interface_mode")

                cmd_result = session.run_command(cmd, require_mode=CliMode.CONFIG)
                result.raw_output[description] = cmd_result.output

                if not cmd_result.success:
                    # Check for idempotent cases (already exists, etc.)
                    output_lower = cmd_result.output.lower()
                    is_idempotent = (
                        "already exist" in output_lower
                        or "existed already" in output_lower
                        or "has been configured" in output_lower
                    )

                    if is_idempotent:
                        logger.info(
                            "Batched mgmt command idempotent (already exists): %s",
                            description,
                        )
                        if not is_navigation:
                            result.steps_completed.append(f"{description} (exists)")
                        continue

                    # Real failure
                    result.error_message = f"{description}: {cmd_result.output[:200]}"
                    if not is_navigation:
                        result.steps_failed.append(description)
                    logger.error(
                        "Batched mgmt setup failed at '%s': %s",
                        description,
                        cmd_result.output[:200],
                    )
                    # Continue with remaining commands to maximize setup
                    # (e.g., TR-069 bind can succeed even if iphost fails)
                    continue

                if not is_navigation:
                    result.steps_completed.append(description)
                    logger.debug(
                        "Batched mgmt command succeeded: %s",
                        description,
                    )

        # Success if no failures, or if critical steps succeeded
        critical_steps = {"configure_iphost", "bind_tr069"}
        failed_critical = set(result.steps_failed) & critical_steps

        if not result.steps_failed:
            result.success = True
            logger.info(
                "Batched management setup complete for ONT %d on %s %s: %d steps",
                spec.ont_id_on_olt,
                olt.name,
                spec.fsp,
                len(result.steps_completed),
            )
        elif not failed_critical:
            # Non-critical failures (internet-config, wan-config can fail on some ONTs)
            result.success = True
            logger.warning(
                "Batched management setup partial success for ONT %d: "
                "completed=%s, failed=%s",
                spec.ont_id_on_olt,
                result.steps_completed,
                result.steps_failed,
            )
        else:
            logger.warning(
                "Batched management setup failed for ONT %d: %s",
                spec.ont_id_on_olt,
                result.error_message,
            )

    except Exception as e:
        logger.exception("Batched management setup failed: %s", e)
        result.error_message = str(e)

    return result


def create_batched_mgmt_spec_from_config_pack(
    config_pack,
    fsp: str,
    ont_id_on_olt: int,
    *,
    allocated_ip: str | None = None,
    subnet_mask: str | None = None,
    gateway: str | None = None,
) -> BatchedMgmtSpec:
    """Create BatchedMgmtSpec from OLT Config Pack.

    Args:
        config_pack: OltConfigPack instance
        fsp: Frame/Slot/Port string
        ont_id_on_olt: ONT ID on the OLT
        allocated_ip: Allocated management IP (for static mode)
        subnet_mask: Subnet mask (for static mode)
        gateway: Gateway IP (for static mode)

    Returns:
        BatchedMgmtSpec ready for execution
    """
    mgmt_vlan_tag = config_pack.management_vlan.tag

    # Determine IP mode
    if allocated_ip and subnet_mask and gateway:
        ip_mode = "static"
    else:
        ip_mode = "dhcp"

    return BatchedMgmtSpec(
        fsp=fsp,
        ont_id_on_olt=ont_id_on_olt,
        mgmt_vlan_tag=mgmt_vlan_tag,
        mgmt_gem_index=config_pack.mgmt_gem_index,
        ip_mode=ip_mode,
        ip_address=allocated_ip,
        subnet_mask=subnet_mask,
        gateway=gateway,
        ip_index=0,
        internet_config_ip_index=config_pack.internet_config_ip_index,
        wan_config_profile_id=config_pack.wan_config_profile_id or None,
        tr069_profile_id=config_pack.tr069_olt_profile_id,
    )
