"""Huawei OLT CLI command generation from provisioning profiles.

Pure function module — no SSH connections, no database queries.
Takes a provisioning profile + ONT context → generates Huawei CLI command sets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OltCommandSet:
    """A group of related OLT CLI commands with metadata."""

    step: str  # Human-readable step name
    commands: list[str]
    description: str = ""
    requires_config_mode: bool = True


@dataclass
class OntProvisioningContext:
    """All context needed to generate provisioning commands for an ONT."""

    # ONT location on OLT
    frame: int
    slot: int
    port: int
    ont_id: int

    # OLT info
    olt_name: str = ""

    # Subscriber info (for template rendering)
    subscriber_code: str = ""
    subscriber_name: str = ""

    # PPPoE
    pppoe_username: str = ""
    pppoe_password: str = ""

    @property
    def fsp(self) -> str:
        return f"{self.frame}/{self.slot}/{self.port}"

    @property
    def frame_slot(self) -> str:
        return f"{self.frame}/{self.slot}"


@dataclass
class WanServiceSpec:
    """Specification for a single WAN service from the provisioning profile."""

    service_type: str  # internet, iptv, voip, management
    vlan_id: int
    gem_index: int
    connection_type: str = "pppoe"  # pppoe, dhcp, static
    pppoe_username_template: str = ""
    pppoe_password: str = ""
    cos_priority: int | None = None
    c_vlan: int | None = None
    nat_enabled: bool = True


@dataclass
class ProvisioningSpec:
    """Full provisioning specification derived from a profile + context."""

    wan_services: list[WanServiceSpec] = field(default_factory=list)
    mgmt_vlan_tag: int | None = None
    mgmt_ip_mode: str = "dhcp"  # dhcp or static
    mgmt_ip_address: str = ""
    mgmt_subnet: str = ""
    mgmt_gateway: str = ""
    tr069_profile_id: int | None = None
    line_profile_id: int = 1
    service_profile_id: int = 1


def _render_template(template: str, context: OntProvisioningContext) -> str:
    """Render a simple template string with subscriber context.

    Supports: {subscriber_code}, {subscriber_name}, {ont_id}
    """
    if not template:
        return template
    result = template
    result = result.replace("{subscriber_code}", context.subscriber_code)
    result = result.replace("{subscriber_name}", context.subscriber_name)
    result = result.replace("{ont_id}", str(context.ont_id))
    return result


class HuaweiCommandGenerator:
    """Generates Huawei OLT CLI commands from provisioning specifications."""

    @staticmethod
    def generate_service_port_commands(
        spec: ProvisioningSpec,
        context: OntProvisioningContext,
    ) -> list[OltCommandSet]:
        """Generate service-port creation commands for all WAN services."""
        if not spec.wan_services:
            return []

        commands: list[str] = []
        for ws in spec.wan_services:
            cmd = (
                f"service-port vlan {ws.vlan_id} gpon {context.fsp} "
                f"ont {context.ont_id} gemport {ws.gem_index} "
                f"multi-service user-vlan {ws.vlan_id} tag-transform translate"
            )
            commands.append(cmd)

        return [
            OltCommandSet(
                step="Create Service Ports",
                commands=commands,
                description=(
                    f"Create {len(commands)} service-port(s) for ONT {context.ont_id} "
                    f"on {context.fsp}"
                ),
            )
        ]

    @staticmethod
    def generate_iphost_commands(
        spec: ProvisioningSpec,
        context: OntProvisioningContext,
    ) -> list[OltCommandSet]:
        """Generate ONT management IP (IPHOST) configuration commands."""
        if not spec.mgmt_vlan_tag:
            return []

        enter_cmd = f"interface gpon {context.frame_slot}"

        if spec.mgmt_ip_mode == "dhcp":
            iphost_cmd = (
                f"ont ipconfig {context.port} {context.ont_id} "
                f"ip-index 0 dhcp vlan {spec.mgmt_vlan_tag}"
            )
        else:
            iphost_cmd = (
                f"ont ipconfig {context.port} {context.ont_id} "
                f"ip-index 0 static ip-address {spec.mgmt_ip_address} "
                f"mask {spec.mgmt_subnet} gateway {spec.mgmt_gateway} "
                f"vlan {spec.mgmt_vlan_tag}"
            )

        return [
            OltCommandSet(
                step="Configure Management IP",
                commands=[enter_cmd, iphost_cmd, "quit"],
                description=(
                    f"Set ONT {context.ont_id} management IP via "
                    f"{'DHCP' if spec.mgmt_ip_mode == 'dhcp' else 'static'} "
                    f"on VLAN {spec.mgmt_vlan_tag}"
                ),
            )
        ]

    @staticmethod
    def generate_tr069_binding_commands(
        spec: ProvisioningSpec,
        context: OntProvisioningContext,
    ) -> list[OltCommandSet]:
        """Generate TR-069 server profile binding commands."""
        if spec.tr069_profile_id is None:
            return []

        enter_cmd = f"interface gpon {context.frame_slot}"
        bind_cmd = (
            f"ont tr069-server-config {context.port} {context.ont_id} "
            f"profile-id {spec.tr069_profile_id}"
        )

        return [
            OltCommandSet(
                step="Bind TR-069 Profile",
                commands=[enter_cmd, bind_cmd, "quit"],
                description=(
                    f"Bind TR-069 server profile {spec.tr069_profile_id} "
                    f"to ONT {context.ont_id}"
                ),
            )
        ]

    @staticmethod
    def generate_full_provisioning(
        spec: ProvisioningSpec,
        context: OntProvisioningContext,
    ) -> list[OltCommandSet]:
        """Generate all provisioning commands in sequence."""
        result: list[OltCommandSet] = []
        result.extend(
            HuaweiCommandGenerator.generate_service_port_commands(spec, context)
        )
        result.extend(
            HuaweiCommandGenerator.generate_iphost_commands(spec, context)
        )
        result.extend(
            HuaweiCommandGenerator.generate_tr069_binding_commands(spec, context)
        )
        return result


def build_spec_from_profile(
    profile: Any,
    context: OntProvisioningContext,
    *,
    tr069_profile_id: int | None = None,
) -> ProvisioningSpec:
    """Build a ProvisioningSpec from an OntProvisioningProfile model instance.

    Args:
        profile: OntProvisioningProfile model instance.
        context: ONT provisioning context for template rendering.
        tr069_profile_id: OLT-level TR-069 server profile ID to bind.

    Returns:
        ProvisioningSpec ready for command generation.
    """
    wan_services: list[WanServiceSpec] = []
    for i, ws in enumerate(getattr(profile, "wan_services", []), start=1):
        if not ws.is_active:
            continue
        vlan_id = ws.s_vlan or ws.c_vlan or 0
        if not vlan_id:
            continue

        username = ""
        if ws.pppoe_username_template:
            username = _render_template(ws.pppoe_username_template, context)

        wan_services.append(
            WanServiceSpec(
                service_type=ws.service_type.value if hasattr(ws.service_type, "value") else str(ws.service_type),
                vlan_id=vlan_id,
                gem_index=ws.gem_port_id or i,
                connection_type=ws.connection_type.value if hasattr(ws.connection_type, "value") else str(ws.connection_type),
                pppoe_username_template=ws.pppoe_username_template or "",
                pppoe_password=ws.pppoe_static_password or "",
                cos_priority=ws.cos_priority,
                c_vlan=ws.c_vlan,
                nat_enabled=ws.nat_enabled,
            )
        )

    mgmt_ip_mode = "dhcp"
    if profile.mgmt_ip_mode and hasattr(profile.mgmt_ip_mode, "value"):
        mgmt_ip_mode = profile.mgmt_ip_mode.value

    return ProvisioningSpec(
        wan_services=wan_services,
        mgmt_vlan_tag=profile.mgmt_vlan_tag,
        mgmt_ip_mode=mgmt_ip_mode,
        tr069_profile_id=tr069_profile_id,
    )
