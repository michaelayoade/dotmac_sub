"""Huawei OLT CLI command generation from provisioning profiles.

Pure function module — no SSH connections, no database queries.
Takes a provisioning profile + ONT context → generates Huawei CLI command sets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.services.credential_crypto import decrypt_credential

logger = logging.getLogger(__name__)


def _enum_value(raw: Any) -> str:
    """Return an enum `.value` when present, otherwise coerce to string."""
    value = getattr(raw, "value", raw)
    return str(value or "")


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
    pppoe_password_mode: str = ""
    cos_priority: int | None = None
    c_vlan: int | None = None
    nat_enabled: bool = True
    user_vlan: int | str | None = None
    tag_transform: str = "translate"
    tcont_profile: str = ""  # T-CONT traffic profile name on OLT
    ip_protocol: str = "ipv4"  # ipv4, dual_stack


@dataclass
class ProvisioningSpec:
    """Full provisioning specification derived from a profile + context."""

    wan_services: list[WanServiceSpec] = field(default_factory=list)
    mgmt_vlan_tag: int | None = None
    mgmt_ip_mode: str = "dhcp"  # dhcp or static
    mgmt_ip_address: str = ""
    mgmt_subnet: str = ""
    mgmt_gateway: str = ""
    mgmt_priority: int | None = None
    tr069_profile_id: int | None = None
    line_profile_id: int | None = None
    service_profile_id: int | None = None
    internet_config_ip_index: int | None = None
    wan_config_profile_id: int | None = None
    pppoe_omci_vlan: int | None = None
    ipv6_enabled: bool = False


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
    def generate_tcont_gem_commands(
        spec: ProvisioningSpec,
        context: OntProvisioningContext,
    ) -> list[OltCommandSet]:
        """Generate T-CONT and GEM port creation commands.

        On Huawei OLTs, T-CONTs and GEM ports are normally defined in the
        line profile and auto-created when the ONT is registered. This method
        generates per-ONT T-CONT/GEM commands for cases where the profile
        specifies explicit GEM port IDs or T-CONT profiles (OMCI config method).

        T-CONT types:
            1 = fixed bandwidth
            2 = assured bandwidth
            3 = non-assured bandwidth (most common for ISP)
            4 = best-effort
            5 = mixed (assured + best-effort)
        """
        if not spec.wan_services:
            return []

        # Collect unique (tcont_profile, gem_index) pairs from WAN services
        tcont_gem_pairs: list[tuple[str, int]] = []
        seen_gems: set[int] = set()
        for ws in spec.wan_services:
            if ws.gem_index not in seen_gems:
                seen_gems.add(ws.gem_index)
                # Derive T-CONT name from the WAN service spec
                tcont_name = getattr(ws, "tcont_profile", None) or ""
                tcont_gem_pairs.append((tcont_name, ws.gem_index))

        if not tcont_gem_pairs:
            return []

        enter_cmd = f"interface gpon {context.frame_slot}"
        commands: list[str] = [enter_cmd]

        for idx, (tcont_name, gem_index) in enumerate(tcont_gem_pairs):
            tcont_id = idx  # T-CONT IDs are 0-based per ONT
            # Create T-CONT (type 3 = non-assured, typical for ISP traffic)
            if tcont_name:
                commands.append(
                    f"ont traffic-table ip-index {context.port} {context.ont_id} "
                    f"{tcont_id} profile-name {tcont_name}"
                )
            # Map GEM port to T-CONT
            commands.append(
                f"ont gemport {context.port} {context.ont_id} "
                f"{gem_index} tcont {tcont_id}"
            )

        commands.append("quit")

        return [
            OltCommandSet(
                step="Create T-CONTs and GEM Ports",
                commands=commands,
                description=(
                    f"Create {len(tcont_gem_pairs)} T-CONT/GEM mapping(s) "
                    f"for ONT {context.ont_id} on {context.fsp}"
                ),
            )
        ]

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
            cmd = build_service_port_command(
                fsp=context.fsp,
                ont_id=context.ont_id,
                gem_index=ws.gem_index,
                vlan_id=ws.vlan_id,
                user_vlan=ws.user_vlan,
                tag_transform=ws.tag_transform,
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

        priority_clause = (
            f" priority {spec.mgmt_priority}" if spec.mgmt_priority is not None else ""
        )
        if spec.mgmt_ip_mode == "dhcp":
            iphost_cmd = (
                f"ont ipconfig {context.port} {context.ont_id} "
                f"ip-index 0 dhcp vlan {spec.mgmt_vlan_tag}{priority_clause}"
            )
        else:
            iphost_cmd = (
                f"ont ipconfig {context.port} {context.ont_id} "
                f"ip-index 0 static ip-address {spec.mgmt_ip_address} "
                f"mask {spec.mgmt_subnet} gateway {spec.mgmt_gateway} "
                f"vlan {spec.mgmt_vlan_tag}{priority_clause}"
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
    def generate_internet_config_commands(
        spec: ProvisioningSpec,
        context: OntProvisioningContext,
    ) -> list[OltCommandSet]:
        """Generate internet-config command to activate ONT TCP stack."""
        if spec.internet_config_ip_index is None:
            return []

        enter_cmd = f"interface gpon {context.frame_slot}"
        ic_cmd = (
            f"ont internet-config {context.port} {context.ont_id} "
            f"ip-index {spec.internet_config_ip_index}"
        )
        return [
            OltCommandSet(
                step="Activate Internet Config",
                commands=[enter_cmd, ic_cmd, "quit"],
                description=(
                    f"Activate TCP stack on ONT {context.ont_id} "
                    f"(ip-index {spec.internet_config_ip_index})"
                ),
            )
        ]

    @staticmethod
    def generate_wan_config_commands(
        spec: ProvisioningSpec,
        context: OntProvisioningContext,
    ) -> list[OltCommandSet]:
        """Generate wan-config command for route+NAT mode."""
        if spec.wan_config_profile_id is None:
            return []

        enter_cmd = f"interface gpon {context.frame_slot}"
        wc_cmd = (
            f"ont wan-config {context.port} {context.ont_id} "
            f"ip-index {spec.internet_config_ip_index or 0} "
            f"profile-id {spec.wan_config_profile_id}"
        )
        return [
            OltCommandSet(
                step="Set WAN Route+NAT Mode",
                commands=[enter_cmd, wc_cmd, "quit"],
                description=(
                    f"Set route+NAT mode on ONT {context.ont_id} "
                    f"(profile-id {spec.wan_config_profile_id})"
                ),
            )
        ]

    @staticmethod
    def generate_pppoe_omci_commands(
        spec: ProvisioningSpec,
        context: OntProvisioningContext,
    ) -> list[OltCommandSet]:
        """Generate PPPoE-over-OMCI configuration commands."""
        if not spec.pppoe_omci_vlan:
            return []

        pppoe_services = [
            ws for ws in spec.wan_services if ws.connection_type == "pppoe"
        ]
        if not pppoe_services:
            return []

        enter_cmd = f"interface gpon {context.frame_slot}"
        commands = [enter_cmd]
        for i, ws in enumerate(pppoe_services, start=1):
            username_template = ws.pppoe_username_template or ""
            username = (
                _render_template(username_template, context)
                if username_template
                else ""
            )
            password = ws.pppoe_password or ""
            if not username or not password:
                continue
            cmd = (
                f"ont ipconfig {context.port} {context.ont_id} "
                f"ip-index {i} pppoe vlan {spec.pppoe_omci_vlan} "
                f"priority {ws.cos_priority or 0} "
                f"user {username} password {password}"
            )
            commands.append(cmd)

        if len(commands) <= 1:
            return []

        commands.append("quit")
        return [
            OltCommandSet(
                step="Configure PPPoE via OMCI",
                commands=commands,
                description=(
                    f"Configure {len(commands) - 2} PPPoE service(s) via OMCI "
                    f"on ONT {context.ont_id}"
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
        gen = HuaweiCommandGenerator
        result: list[OltCommandSet] = []
        result.extend(gen.generate_tcont_gem_commands(spec, context))
        result.extend(gen.generate_service_port_commands(spec, context))
        result.extend(gen.generate_iphost_commands(spec, context))
        result.extend(gen.generate_internet_config_commands(spec, context))
        result.extend(gen.generate_wan_config_commands(spec, context))
        result.extend(gen.generate_tr069_binding_commands(spec, context))
        result.extend(gen.generate_pppoe_omci_commands(spec, context))
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

        raw_vlan_mode = getattr(ws, "vlan_mode", None)
        vlan_mode = _enum_value(raw_vlan_mode)
        user_vlan: int | str | None = None
        tag_transform = "translate"
        if vlan_mode == "translate" and ws.s_vlan and ws.c_vlan:
            vlan_id = ws.s_vlan
            user_vlan = ws.c_vlan
            tag_transform = "translate"
        elif vlan_mode == "transparent":
            tag_transform = "transparent"
            user_vlan = "untagged"
        elif vlan_mode == "untagged":
            tag_transform = "default"
            user_vlan = "untagged"
        else:
            user_vlan = ws.c_vlan or vlan_id

        raw_password_mode = getattr(ws, "pppoe_password_mode", None)
        password_mode = _enum_value(raw_password_mode)

        wan_services.append(
            WanServiceSpec(
                service_type=_enum_value(ws.service_type),
                vlan_id=vlan_id,
                gem_index=ws.gem_port_id or i,
                connection_type=_enum_value(ws.connection_type),
                pppoe_username_template=ws.pppoe_username_template or "",
                pppoe_password=decrypt_credential(ws.pppoe_static_password) or "",
                pppoe_password_mode=password_mode,
                cos_priority=ws.cos_priority,
                c_vlan=ws.c_vlan,
                nat_enabled=ws.nat_enabled,
                user_vlan=user_vlan,
                tag_transform=tag_transform,
                tcont_profile=ws.t_cont_profile or "",
            )
        )

    mgmt_ip_mode = "dhcp"
    if profile.mgmt_ip_mode and hasattr(profile.mgmt_ip_mode, "value"):
        mgmt_ip_mode = profile.mgmt_ip_mode.value

    internet_config_ip_index: int | None = None
    wan_config_profile_id: int | None = None
    pppoe_omci_vlan: int | None = None
    mgmt_priority: int | None = None
    line_profile_id: int | None = None
    service_profile_id: int | None = None

    if hasattr(profile, "internet_config_ip_index"):
        raw_ic = getattr(profile, "internet_config_ip_index", None)
        internet_config_ip_index = int(raw_ic) if raw_ic is not None else None
    if hasattr(profile, "wan_config_profile_id"):
        raw_wc = getattr(profile, "wan_config_profile_id", None)
        wan_config_profile_id = int(raw_wc) if raw_wc is not None else None
    if hasattr(profile, "authorization_line_profile_id"):
        raw_line = getattr(profile, "authorization_line_profile_id", None)
        line_profile_id = int(raw_line) if raw_line is not None else None
    if hasattr(profile, "authorization_service_profile_id"):
        raw_service = getattr(profile, "authorization_service_profile_id", None)
        service_profile_id = int(raw_service) if raw_service is not None else None
    if hasattr(profile, "pppoe_omci_vlan"):
        raw_pv = getattr(profile, "pppoe_omci_vlan", None)
        pppoe_omci_vlan = int(raw_pv) if raw_pv is not None else None
    for ws in getattr(profile, "wan_services", []) or []:
        if _enum_value(getattr(ws, "service_type", "")) != "management":
            continue
        raw_priority = getattr(ws, "cos_priority", None)
        if raw_priority is not None:
            mgmt_priority = int(raw_priority)
            break

    # Determine if dual-stack is enabled from profile ip_protocol
    ipv6_enabled = False
    if hasattr(profile, "ip_protocol") and profile.ip_protocol:
        ip_proto = (
            profile.ip_protocol.value
            if hasattr(profile.ip_protocol, "value")
            else str(profile.ip_protocol)
        )
        ipv6_enabled = ip_proto == "dual_stack"

    return ProvisioningSpec(
        wan_services=wan_services,
        mgmt_vlan_tag=profile.mgmt_vlan_tag,
        mgmt_ip_mode=mgmt_ip_mode,
        mgmt_priority=mgmt_priority,
        tr069_profile_id=tr069_profile_id,
        line_profile_id=line_profile_id,
        service_profile_id=service_profile_id,
        internet_config_ip_index=internet_config_ip_index,
        wan_config_profile_id=wan_config_profile_id,
        pppoe_omci_vlan=pppoe_omci_vlan,
        ipv6_enabled=ipv6_enabled,
    )


def build_service_port_command(
    *,
    fsp: str,
    ont_id: int,
    gem_index: int,
    vlan_id: int,
    user_vlan: int | str | None = None,
    tag_transform: str = "translate",
    port_index: int | None = None,
) -> str:
    """Build a Huawei service-port command preserving modeled VLAN intent.

    Args:
        fsp: Frame/Slot/Port string (e.g., "0/1/0")
        ont_id: ONT ID on the PON port
        gem_index: GEM port index
        vlan_id: Service VLAN ID
        user_vlan: User VLAN (default: same as vlan_id)
        tag_transform: VLAN tag transform mode (default: translate)
        port_index: Pre-allocated service-port index. If provided, creates
                    a service-port with explicit index. If None, OLT auto-assigns.

    Returns:
        Huawei CLI command string for service-port creation
    """
    resolved_user_vlan = user_vlan
    if resolved_user_vlan is None:
        resolved_user_vlan = vlan_id

    if port_index is not None:
        # Use pre-allocated index from DB allocator (Phase 1)
        return (
            f"service-port {port_index} vlan {vlan_id} gpon {fsp} "
            f"ont {ont_id} gemport {gem_index} "
            f"multi-service user-vlan {resolved_user_vlan} "
            f"tag-transform {tag_transform}"
        )
    else:
        # Legacy: auto-assign index
        return (
            f"service-port vlan {vlan_id} gpon {fsp} "
            f"ont {ont_id} gemport {gem_index} "
            f"multi-service user-vlan {resolved_user_vlan} "
            f"tag-transform {tag_transform}"
        )
