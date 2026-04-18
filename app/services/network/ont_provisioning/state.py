"""ONT provisioning state dataclasses for state reconciliation.

This module defines the desired and actual state representations used for
idempotent ONT provisioning. Instead of imperatively issuing commands, the
system computes the delta between desired and actual state, then applies
only the necessary changes.

Key concepts:
- DesiredOntState: Built from OntProvisioningProfile (VLANs from profile WAN services)
- ActualOntState: Read from OLT via SSH
- ProvisioningDelta: Computed changes needed to reconcile actual -> desired
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models.network import OLTDevice, OntProvisioningProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ProvisioningAction(Enum):
    """Action to take for a resource during provisioning."""

    CREATE = "create"
    DELETE = "delete"
    NOOP = "noop"


class ServicePortMatchResult(Enum):
    """Result of matching a desired service port against actual state."""

    EXACT_MATCH = "exact_match"  # Same VLAN, GEM, tag_transform
    PARTIAL_MATCH = "partial_match"  # Same VLAN, GEM but different tag_transform
    NOT_FOUND = "not_found"


# ---------------------------------------------------------------------------
# Desired State (from Profile)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesiredServicePort:
    """A single service-port specification from the provisioning profile.

    Built from OntProfileWanService records, NOT cloned from reference ONTs.
    """

    vlan_id: int
    gem_index: int
    user_vlan: int | str | None = None
    tag_transform: str = "translate"

    def matches(self, actual: ActualServicePort) -> ServicePortMatchResult:
        """Check if this desired port matches an actual port."""
        if self.vlan_id != actual.vlan_id or self.gem_index != actual.gem_index:
            return ServicePortMatchResult.NOT_FOUND
        # Check tag_transform if available on actual
        if actual.tag_transform and self.tag_transform != actual.tag_transform:
            return ServicePortMatchResult.PARTIAL_MATCH
        return ServicePortMatchResult.EXACT_MATCH


@dataclass(frozen=True)
class DesiredManagementConfig:
    """Management plane configuration from profile."""

    vlan_tag: int
    ip_mode: str = "dhcp"  # "dhcp" or "static"
    ip_address: str | None = None
    subnet: str | None = None
    gateway: str | None = None
    priority: int | None = None


@dataclass(frozen=True)
class DesiredTr069Config:
    """TR-069 configuration from profile."""

    olt_profile_id: int
    cr_username: str | None = None
    cr_password: str | None = None


@dataclass(frozen=True)
class DesiredOntState:
    """Complete desired state for an ONT, built from profile.

    This represents what the ONT should look like when fully provisioned.
    VLANs come from profile WAN services, NOT cloned from reference ONTs.
    """

    ont_id: str
    serial_number: str
    fsp: str
    olt_ont_id: int
    line_profile_id: int | None = None
    service_profile_id: int | None = None
    service_ports: tuple[DesiredServicePort, ...] = field(default_factory=tuple)
    management: DesiredManagementConfig | None = None
    tr069: DesiredTr069Config | None = None
    internet_config_ip_index: int | None = None
    wan_config_profile_id: int | None = None


# ---------------------------------------------------------------------------
# Actual State (from OLT)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActualServicePort:
    """A single service-port as read from the OLT."""

    index: int  # OLT-assigned service-port index
    vlan_id: int
    gem_index: int
    ont_id: int  # OLT ONT-ID
    state: str  # "up" or "down"
    fsp: str = ""
    tag_transform: str = ""


@dataclass(frozen=True)
class ActualOntState:
    """Current ONT state as read from the OLT."""

    is_authorized: bool
    olt_ont_id: int | None
    service_ports: tuple[ActualServicePort, ...] = field(default_factory=tuple)
    mgmt_vlan: int | None = None
    tr069_profile_id: int | None = None


# ---------------------------------------------------------------------------
# Delta (Computed Changes)
# ---------------------------------------------------------------------------


@dataclass
class ServicePortDelta:
    """Change needed for a single service port."""

    action: ProvisioningAction
    desired: DesiredServicePort | None
    actual: ActualServicePort | None
    message: str = ""
    depends_on_delete_index: int | None = None  # Index of DELETE delta this CREATE depends on


@dataclass
class ProvisioningDelta:
    """Complete set of changes needed to reconcile actual -> desired state."""

    service_port_deltas: list[ServicePortDelta] = field(default_factory=list)
    needs_mgmt_ip_config: bool = False
    needs_tr069_bind: bool = False
    needs_internet_config: bool = False
    needs_wan_config: bool = False

    # Validation results (set by validate_delta)
    optical_budget_ok: bool = True
    optical_budget_message: str = ""
    mgmt_vlan_trunked: bool = True
    mgmt_vlan_message: str = ""
    service_vlans_ok: bool = True
    service_vlans_message: str = ""
    ip_index_valid: bool = True
    ip_index_message: str = ""

    @property
    def has_changes(self) -> bool:
        """Return True if there are any changes to apply."""
        has_port_changes = any(
            d.action != ProvisioningAction.NOOP for d in self.service_port_deltas
        )
        return (
            has_port_changes
            or self.needs_mgmt_ip_config
            or self.needs_tr069_bind
            or self.needs_internet_config
            or self.needs_wan_config
        )

    @property
    def is_valid(self) -> bool:
        """Return True if all validations passed."""
        return (
            self.optical_budget_ok
            and self.mgmt_vlan_trunked
            and self.service_vlans_ok
            and self.ip_index_valid
        )


# ---------------------------------------------------------------------------
# State Building Functions
# ---------------------------------------------------------------------------


def build_desired_state_from_profile(
    db: Session,
    ont_id: str,
    profile: OntProvisioningProfile | None = None,
    tr069_olt_profile_id: int | None = None,
) -> tuple[DesiredOntState | None, str]:
    """Build desired state from profile WAN services.

    IMPORTANT: This function reads VLANs from the profile's WAN services,
    NOT from a reference ONT. This eliminates the "reference cloning" problem
    where errors propagate from misconfigured reference ONTs.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
    profile: Optional explicit profile. If None, uses ONT's assigned profile.
        tr069_olt_profile_id: Optional OLT-local TR-069 profile ID. When omitted,
            the effective OLT profile is resolved from the ONT/OLT.

    Returns:
        Tuple of (DesiredOntState or None, error message).
    """
    from app.models.network import OntUnit
    from app.services.network.ont_provisioning.context import resolve_olt_context
    from app.services.network.ont_provisioning.profiles import resolve_profile

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return None, "ONT not found"

    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return None, err

    # Resolve profile
    resolved_profile = profile or resolve_profile(db, ont)
    if not resolved_profile:
        return None, "No provisioning profile assigned or specified"

    # Build service ports from profile WAN services
    service_ports = _build_service_ports_from_profile(resolved_profile)

    # Build management config
    management = None
    if resolved_profile.mgmt_vlan_tag:
        mgmt_ip_mode = (
            resolved_profile.mgmt_ip_mode.value
            if resolved_profile.mgmt_ip_mode
            else "dhcp"
        )
        subnet = None
        gateway = None
        if mgmt_ip_mode == "static_ip" and resolved_profile.mgmt_ip_pool_id:
            import ipaddress

            pool = getattr(resolved_profile, "mgmt_ip_pool", None)
            if pool is None:
                from app.models.network import IpPool

                pool = db.get(IpPool, resolved_profile.mgmt_ip_pool_id)
            if pool is not None:
                gateway = getattr(pool, "gateway", None)
                try:
                    subnet = str(ipaddress.ip_network(str(pool.cidr), strict=False).netmask)
                except ValueError:
                    subnet = None
        management = DesiredManagementConfig(
            vlan_tag=resolved_profile.mgmt_vlan_tag,
            ip_mode=mgmt_ip_mode,
            ip_address=getattr(ont, "mgmt_ip_address", None),
            subnet=subnet,
            gateway=gateway,
        )

    # Build TR-069 config if OLT has ACS
    tr069 = None
    if ctx.olt.tr069_acs_server_id:
        resolved_tr069_profile_id = tr069_olt_profile_id
        if resolved_tr069_profile_id is None:
            try:
                from app.services.web_network_onts import (
                    resolve_effective_tr069_profile_for_ont,
                )

                tr069_profile, tr069_error = resolve_effective_tr069_profile_for_ont(
                    db, ont
                )
                resolved_tr069_profile_id = getattr(tr069_profile, "profile_id", None)
                if resolved_tr069_profile_id is None:
                    return None, tr069_error or "No OLT TR-069 profile resolved"
            except Exception as exc:
                return None, f"Failed to resolve OLT TR-069 profile: {exc}"

        tr069 = DesiredTr069Config(
            olt_profile_id=int(resolved_tr069_profile_id),
            cr_username=resolved_profile.cr_username,
            cr_password=resolved_profile.cr_password,
        )

    return (
        DesiredOntState(
            ont_id=ont_id,
            serial_number=ont.serial_number or "",
            fsp=ctx.fsp,
            olt_ont_id=ctx.olt_ont_id,
            line_profile_id=resolved_profile.authorization_line_profile_id,
            service_profile_id=resolved_profile.authorization_service_profile_id,
            service_ports=tuple(service_ports),
            management=management,
            tr069=tr069,
            internet_config_ip_index=resolved_profile.internet_config_ip_index,
            wan_config_profile_id=resolved_profile.wan_config_profile_id,
        ),
        "",
    )


def _build_service_ports_from_profile(
    profile: OntProvisioningProfile,
) -> list[DesiredServicePort]:
    """Extract service port specifications from profile WAN services.

    Reads VLANs from OntProfileWanService records attached to the profile.
    Each WAN service with a configured s_vlan (outer VLAN) becomes a service port.
    """
    service_ports: list[DesiredServicePort] = []

    wan_services = getattr(profile, "wan_services", []) or []
    for service in wan_services:
        if not getattr(service, "is_active", True):
            continue

        s_vlan = getattr(service, "s_vlan", None)
        if s_vlan is None:
            continue

        gem_index = getattr(service, "gem_port_id", None) or 1
        c_vlan = getattr(service, "c_vlan", None)

        # Determine user_vlan and tag_transform based on vlan_mode
        vlan_mode = getattr(service, "vlan_mode", None)
        vlan_mode_value: str = ""
        if vlan_mode is not None and hasattr(vlan_mode, "value"):
            vlan_mode_value = str(vlan_mode.value)
        elif vlan_mode is not None:
            vlan_mode_value = str(vlan_mode)

        user_vlan: int | str | None = None
        tag_transform = "translate"

        if vlan_mode_value == "untagged":
            user_vlan = "untagged"
            tag_transform = "default"
        elif vlan_mode_value == "transparent":
            user_vlan = "transparent"
            tag_transform = "transparent"
        elif c_vlan is not None:
            user_vlan = c_vlan

        service_ports.append(
            DesiredServicePort(
                vlan_id=s_vlan,
                gem_index=gem_index,
                user_vlan=user_vlan,
                tag_transform=tag_transform,
            )
        )

    return service_ports


def read_actual_state(
    olt: OLTDevice,
    fsp: str,
    olt_ont_id: int,
) -> tuple[ActualOntState | None, str]:
    """Read actual ONT state from OLT via single SSH session.

    This function uses a single SSH connection to read all relevant state,
    avoiding the connection exhaustion issue of one connection per command.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port (e.g., "0/2/1").
        olt_ont_id: The ONT-ID on the OLT.

    Returns:
        Tuple of (ActualOntState or None, error message).
    """
    from paramiko.ssh_exception import SSHException

    from app.services.network.olt_ssh import (
        _open_shell,
        _parse_service_port_table,
        _read_until_prompt,
        _run_huawei_cmd,
        _run_huawei_paged_cmd,
        is_error_output,
    )

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return None, f"Connection failed: {exc}"

    try:
        # Enter enable mode
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        # Read service ports for this FSP
        output = _run_huawei_paged_cmd(channel, f"display service-port port {fsp}")
        all_ports = _parse_service_port_table(output)

        # Filter to this ONT's ports
        ont_ports = [p for p in all_ports if p.ont_id == olt_ont_id]

        # Convert to ActualServicePort
        actual_ports = tuple(
            ActualServicePort(
                index=p.index,
                vlan_id=p.vlan_id,
                gem_index=p.gem_index,
                ont_id=p.ont_id,
                state=p.state,
                fsp=p.fsp,
                tag_transform=getattr(p, "tag_transform", ""),
            )
            for p in ont_ports
        )

        parts = fsp.split("/")
        is_authorized = False
        if len(parts) == 3:
            status_output = _run_huawei_cmd(
                channel,
                f"display ont info {parts[0]} {parts[1]} {parts[2]} {olt_ont_id}",
            )
            is_authorized = not is_error_output(status_output)
        if not is_authorized:
            return None, f"ONT {olt_ont_id} is not authorized on OLT port {fsp}"

        return (
            ActualOntState(
                is_authorized=is_authorized,
                olt_ont_id=olt_ont_id,
                service_ports=actual_ports,
            ),
            "",
        )
    except Exception as exc:
        logger.error("Error reading ONT state from OLT %s: %s", olt.name, exc)
        return None, f"Error reading state: {exc}"
    finally:
        transport.close()
