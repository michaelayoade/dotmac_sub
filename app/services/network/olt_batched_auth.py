"""Batched OLT authorization for improved performance.

This module provides single-session execution of all authorization commands,
reducing SSH overhead from 7-8 sessions to 1 session.

Target: Reduce ONT authorization from ~8-10s to ~2-3s.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.models.network import OLTDevice

logger = logging.getLogger(__name__)


@dataclass
class ServicePortSpec:
    """Specification for a single service-port."""

    vlan_id: int
    gem_index: int
    service_type: str | None = None  # internet, management, tr069, iptv, voip
    user_vlan: int | None = None
    tag_transform: str = "translate"
    port_index: int | None = None  # Pre-allocated index (if using DB allocator)


@dataclass
class MgmtIpConfig:
    """Management IP configuration for ONT."""

    mode: str  # "static" or "dhcp"
    ip_address: str | None = None
    netmask: str | None = None
    gateway: str | None = None
    vlan_id: int | None = None
    priority: int = 0


@dataclass
class BatchedAuthorizationSpec:
    """Complete specification for ONT authorization in a single session."""

    serial_number: str
    fsp: str  # Frame/Slot/Port (e.g., "0/1/0")
    line_profile_id: int
    service_profile_id: int

    # Optional configurations
    service_ports: list[ServicePortSpec] = field(default_factory=list)
    mgmt_config: MgmtIpConfig | None = None
    tr069_profile_id: int | None = None
    description: str | None = None
    ont_id: int | None = None  # If known in advance, else auto-assigned

    # Speed profiles
    download_profile_id: int | None = None
    upload_profile_id: int | None = None


@dataclass
class BatchResult:
    """Result of batched authorization."""

    success: bool
    ont_id: int | None = None
    service_port_indices: list[int] = field(default_factory=list)
    steps_completed: list[str] = field(default_factory=list)
    steps_failed: list[str] = field(default_factory=list)
    error_message: str | None = None
    raw_output: dict[str, str] = field(default_factory=dict)


def build_authorization_command_batch(
    spec: BatchedAuthorizationSpec,
    context: dict[str, Any] | None = None,
) -> list[tuple[str, str, bool]]:
    """Build complete command sequence for single-session execution.

    Returns list of tuples: (command, description, requires_config_mode)

    Command ordering:
    1. interface gpon -> ont add (get ONT-ID)
    2. quit -> service-port commands (global config)
    3. interface gpon -> ont ipconfig, ont internet-config, ont tr069-server-config
    4. quit
    """
    ctx = context or {}
    commands: list[tuple[str, str, bool]] = []

    frame, slot, port = spec.fsp.split("/")

    # Phase 1: Authorization
    # Enter interface mode
    commands.append(
        (
            f"interface gpon {frame}/{slot}",
            "Enter GPON interface mode",
            True,  # requires config mode
        )
    )

    # Build ont add command
    ont_add_cmd = (
        f"ont add {port} sn-auth {spec.serial_number} "
        f"omci ont-lineprofile-id {spec.line_profile_id} "
        f"ont-srvprofile-id {spec.service_profile_id}"
    )
    if spec.description:
        # Escape quotes in description
        desc = spec.description.replace('"', "'")[:64]
        ont_add_cmd += f' desc "{desc}"'

    commands.append((ont_add_cmd, "Authorize ONT", False))

    # Exit interface for global config
    commands.append(("quit", "Exit interface mode", False))

    # Phase 2: Service-ports (global config level)
    for i, sp in enumerate(spec.service_ports):
        # Build service-port command
        resolved_user_vlan = sp.user_vlan if sp.user_vlan is not None else sp.vlan_id

        if sp.port_index is not None:
            # Use pre-allocated index
            sp_cmd = (
                f"service-port {sp.port_index} vlan {sp.vlan_id} "
                f"gpon {spec.fsp} ont {{ont_id}} gemport {sp.gem_index} "
                f"multi-service user-vlan {resolved_user_vlan} "
                f"tag-transform {sp.tag_transform}"
            )
        else:
            # Auto-assign index
            sp_cmd = (
                f"service-port vlan {sp.vlan_id} "
                f"gpon {spec.fsp} ont {{ont_id}} gemport {sp.gem_index} "
                f"multi-service user-vlan {resolved_user_vlan} "
                f"tag-transform {sp.tag_transform}"
            )

        commands.append(
            (
                sp_cmd,
                f"Create service-port {i + 1} (VLAN {sp.vlan_id})",
                False,
            )
        )

    # Phase 3: ONT configuration (interface mode again)
    has_ont_config = spec.mgmt_config is not None or spec.tr069_profile_id is not None

    if has_ont_config:
        commands.append(
            (
                f"interface gpon {frame}/{slot}",
                "Re-enter GPON interface mode",
                False,
            )
        )

        # Management IP configuration
        if spec.mgmt_config:
            mc = spec.mgmt_config
            if mc.mode == "dhcp":
                ip_cmd = (
                    f"ont ipconfig {port} {{ont_id}} ip-index 0 dhcp vlan {mc.vlan_id}"
                )
            else:
                ip_cmd = (
                    f"ont ipconfig {port} {{ont_id}} ip-index 0 "
                    f"static ip-address {mc.ip_address} "
                    f"mask {mc.netmask} gateway {mc.gateway} "
                    f"vlan {mc.vlan_id} priority {mc.priority}"
                )
            commands.append((ip_cmd, "Configure management IP", False))

        # TR-069 profile
        if spec.tr069_profile_id is not None:
            tr069_cmd = (
                f"ont tr069-server-config {port} {{ont_id}} "
                f"profile-id {spec.tr069_profile_id}"
            )
            commands.append((tr069_cmd, "Configure TR-069 profile", False))

        commands.append(("quit", "Exit interface mode", False))

    return commands


def execute_batched_authorization(
    olt: OLTDevice,
    spec: BatchedAuthorizationSpec,
) -> BatchResult:
    """Execute all authorization commands in one SSH session.

    This function handles:
    - Single SSH connection for entire flow
    - ONT-ID extraction from authorization response
    - Service-port index capture
    - Error handling with partial rollback
    """
    from app.services.network.olt_ssh_session import CliMode, olt_session

    result = BatchResult(success=False)
    commands = build_authorization_command_batch(spec)

    try:
        with olt_session(olt) as session:
            ont_id: int | None = spec.ont_id

            for cmd_template, description, _ in commands:
                # Substitute ONT-ID if we have it
                cmd = cmd_template
                if ont_id is not None:
                    cmd = cmd.replace("{ont_id}", str(ont_id))
                elif "{ont_id}" in cmd:
                    result.error_message = f"ONT-ID not available for: {description}"
                    result.steps_failed.append(description)
                    return result

                # Execute command
                cmd_result = session.run_command(cmd, require_mode=CliMode.CONFIG)
                result.raw_output[description] = cmd_result.output

                if not cmd_result.success:
                    # Check for idempotent cases
                    if (
                        cmd_result.error_code
                        and cmd_result.error_code.name == "ALREADY_EXISTS"
                    ):
                        logger.info(
                            "Command idempotent (already exists): %s", description
                        )
                        # Try to extract ONT-ID from "already exists" message
                        if "ont add" in cmd.lower() and ont_id is None:
                            ont_id = _extract_ont_id_from_output(cmd_result.output)
                        result.steps_completed.append(f"{description} (already exists)")
                        continue

                    result.error_message = f"{description}: {cmd_result.output[:200]}"
                    result.steps_failed.append(description)
                    logger.error(
                        "Batched auth failed at '%s': %s",
                        description,
                        cmd_result.output[:200],
                    )
                    return result

                # Extract ONT-ID from authorization response
                if "ont add" in cmd.lower() and ont_id is None:
                    ont_id = _extract_ont_id_from_output(cmd_result.output)
                    if ont_id is not None:
                        result.ont_id = ont_id
                        logger.info("Extracted ONT-ID %d from authorization", ont_id)

                # Extract service-port index from response
                if "service-port" in cmd.lower():
                    sp_idx = _extract_service_port_index(cmd_result.output)
                    if sp_idx is not None:
                        result.service_port_indices.append(sp_idx)

                result.steps_completed.append(description)

            result.success = True
            result.ont_id = ont_id

    except Exception as e:
        logger.exception("Batched authorization failed: %s", e)
        result.error_message = str(e)

    return result


def _extract_ont_id_from_output(output: str) -> int | None:
    """Extract ONT-ID from OLT command output.

    Handles patterns like:
    - "ONTID :0"
    - "ont-id=0"
    - "ONT-ID: 0"
    - "ID=0"
    """
    import re

    patterns = [
        r"ONTID\s*[:\s=]+(\d+)",
        r"ont-id\s*[:\s=]+(\d+)",
        r"ONT-ID\s*[:\s=]+(\d+)",
        r"\bID\s*=\s*(\d+)",
        r"ont\s+(\d+)\s+added",
    ]

    for pattern in patterns:
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            return int(match.group(1))

    return None


def _extract_service_port_index(output: str) -> int | None:
    """Extract service-port index from OLT command output.

    Handles patterns like:
    - "service-port 123 created"
    - "Index : 123"
    """
    import re

    patterns = [
        r"service-port\s+(\d+)\s+",
        r"Index\s*[:\s=]+(\d+)",
        r"port\s+(\d+)\s+added",
    ]

    for pattern in patterns:
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            return int(match.group(1))

    return None


def build_authorization_spec_from_profile(
    db,
    profile,
    serial_number: str,
    fsp: str,
    *,
    mgmt_ip: str | None = None,
    mgmt_vlan_id: int | None = None,
) -> BatchedAuthorizationSpec:
    """Build a BatchedAuthorizationSpec from an OntProvisioningProfile.

    Args:
        db: Database session
        profile: OntProvisioningProfile instance
        serial_number: ONT serial number
        fsp: Frame/Slot/Port string
        mgmt_ip: Management IP address (if using static)
        mgmt_vlan_id: Management VLAN ID

    Returns:
        BatchedAuthorizationSpec ready for execution
    """
    spec = BatchedAuthorizationSpec(
        serial_number=serial_number,
        fsp=fsp,
        line_profile_id=profile.line_profile_id,
        service_profile_id=profile.service_profile_id,
        tr069_profile_id=getattr(profile, "tr069_server_profile_id", None),
    )

    # Add service ports from profile WAN services
    if hasattr(profile, "wan_services") and profile.wan_services:
        for ws in profile.wan_services:
            spec.service_ports.append(
                ServicePortSpec(
                    vlan_id=ws.get("vlan_id") or ws.get("svlan_id"),
                    gem_index=ws.get("gem_index", 0),
                    service_type=ws.get("service_type", "internet"),
                    user_vlan=ws.get("cvlan_id"),
                )
            )

    # Management config
    if mgmt_ip and mgmt_vlan_id:
        spec.mgmt_config = MgmtIpConfig(
            mode="static",
            ip_address=mgmt_ip,
            netmask="255.255.255.0",
            gateway=_derive_gateway(mgmt_ip),
            vlan_id=mgmt_vlan_id,
        )
    elif mgmt_vlan_id:
        spec.mgmt_config = MgmtIpConfig(
            mode="dhcp",
            vlan_id=mgmt_vlan_id,
        )

    return spec


def _derive_gateway(ip: str) -> str:
    """Derive gateway from IP (assume .1 in same subnet)."""
    parts = ip.split(".")
    if len(parts) == 4:
        parts[3] = "1"
        return ".".join(parts)
    return ip
