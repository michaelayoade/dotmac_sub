"""ONT provisioning state dataclasses for state reconciliation.

This module defines the desired and actual state representations used for
idempotent ONT provisioning. Instead of imperatively issuing commands, the
system computes the delta between desired and actual state, then applies
only the necessary changes.

Key concepts:
- DesiredOntState: Built from OntUnit desired config plus OLT defaults
- ActualOntState: Read from OLT via SSH
- ProvisioningDelta: Computed changes needed to reconcile actual -> desired
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.services.network.effective_ont_config import resolve_effective_ont_config

if TYPE_CHECKING:
    from app.models.network import OLTDevice

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
# Desired State
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesiredServicePort:
    """A single service-port specification for the ONT."""

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
    """Management plane configuration."""

    vlan_tag: int
    ip_mode: str = "dhcp"  # "dhcp" or "static"
    ip_address: str | None = None
    subnet: str | None = None
    gateway: str | None = None
    priority: int | None = None


@dataclass(frozen=True)
class DesiredTr069Config:
    """TR-069 configuration."""

    olt_profile_id: int
    cr_username: str | None = None
    cr_password: str | None = None


@dataclass(frozen=True)
class DesiredOntState:
    """Complete desired state for an ONT."""

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
class ActualManagementConfig:
    """Current management plane configuration read from the OLT."""

    vlan_tag: int
    ip_mode: str
    ip_index: int = 0
    ip_address: str | None = None
    subnet: str | None = None
    gateway: str | None = None


@dataclass(frozen=True)
class ActualOntState:
    """Current ONT state as read from the OLT."""

    is_authorized: bool
    olt_ont_id: int | None
    service_ports: tuple[ActualServicePort, ...] = field(default_factory=tuple)
    management: ActualManagementConfig | None = None
    tr069_profile_id: int | None = None
    internet_config_ip_indices: tuple[int, ...] = field(default_factory=tuple)
    wan_config_profiles: dict[int, int] = field(default_factory=dict)


_INT_RE = re.compile(r"\b(\d+)\b")


def _first_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    match = _INT_RE.search(str(value))
    return int(match.group(1)) if match else None


def _parse_actual_management_config(output: str) -> ActualManagementConfig | None:
    from app.services.network.olt_ssh_ont.iphost import parse_iphost_config_output

    config = parse_iphost_config_output(output)
    vlan = _first_int(config.get("vlan"))
    if vlan is None:
        return None
    raw_mode = str(config.get("mode") or "").strip().lower()
    ip_mode = "static" if "static" in raw_mode else "dhcp"
    return ActualManagementConfig(
        vlan_tag=vlan,
        ip_mode=ip_mode,
        ip_index=_first_int(config.get("ip_index")) or 0,
        ip_address=str(config.get("ip_address") or "") or None,
        subnet=str(config.get("subnet_mask") or "") or None,
        gateway=str(config.get("gateway") or "") or None,
    )


def _parse_ip_indices(output: str) -> tuple[int, ...]:
    values: set[int] = set()
    for line in output.splitlines():
        lowered = line.lower()
        if "ip-index" not in lowered and "ip index" not in lowered:
            continue
        index = _first_int(line)
        if index is not None:
            values.add(index)
    return tuple(sorted(values))


def _parse_tr069_profile_id(output: str) -> int | None:
    for line in output.splitlines():
        lowered = line.lower()
        if "profile" not in lowered:
            continue
        value = _first_int(line)
        if value is not None:
            return value
    return None


def _parse_wan_config_profiles(output: str) -> dict[int, int]:
    profiles: dict[int, int] = {}
    current_ip_index: int | None = None
    for line in output.splitlines():
        lowered = line.lower()
        if "ip-index" in lowered or "ip index" in lowered:
            current_ip_index = _first_int(line)
        if "profile" in lowered:
            profile_id = _first_int(line)
            if current_ip_index is not None and profile_id is not None:
                profiles[current_ip_index] = profile_id
    return profiles


def _safe_display_command(channel, command: str, *, prompt: str) -> str:
    from app.services.network.olt_ssh import _run_huawei_cmd, is_error_output

    try:
        output = _run_huawei_cmd(channel, command, prompt=prompt)
    except Exception as exc:
        logger.debug("OLT display command failed: %s: %s", command, exc)
        return ""
    return "" if is_error_output(output) else output


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
    depends_on_delete_index: int | None = (
        None  # Index of DELETE delta this CREATE depends on
    )


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


def build_desired_state_from_config(
    db: Session,
    ont_id: str,
) -> tuple[DesiredOntState | None, str]:
    """Build desired state from ONT desired config and OLT defaults.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.

    Returns:
        Tuple of (DesiredOntState or None, error message).
    """
    from app.models.network import OntUnit
    from app.services.network.ont_provisioning.context import resolve_olt_context

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return None, "ONT not found"

    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return None, err

    effective = resolve_effective_ont_config(db, ont)
    effective_values = (
        effective.get("values", {}) if isinstance(effective, dict) else {}
    )

    service_ports: list[DesiredServicePort] = []
    if effective_values.get("wan_vlan") is not None:
        service_ports.append(
            DesiredServicePort(
                vlan_id=int(effective_values["wan_vlan"]),
                gem_index=int(effective_values.get("wan_gem_index") or 1),
            )
        )

    # Build management config
    management = None
    mgmt_ip_mode = str(effective_values.get("mgmt_ip_mode") or "").strip()
    if effective_values.get("mgmt_vlan") is not None and mgmt_ip_mode:
        management = DesiredManagementConfig(
            vlan_tag=int(effective_values["mgmt_vlan"]),
            ip_mode=mgmt_ip_mode,
            ip_address=effective_values.get("mgmt_ip_address"),
            subnet=effective_values.get("mgmt_subnet"),
            gateway=effective_values.get("mgmt_gateway"),
        )

    # Build TR-069 config if OLT has ACS
    tr069 = None
    resolved_tr069_profile_id = effective_values.get("tr069_olt_profile_id")
    if effective_values.get("tr069_acs_server_id") and resolved_tr069_profile_id:
        tr069 = DesiredTr069Config(
            olt_profile_id=int(resolved_tr069_profile_id),
            cr_username=effective_values.get("cr_username"),
            cr_password=effective_values.get("cr_password"),
        )

    return (
        DesiredOntState(
            ont_id=ont_id,
            serial_number=ont.serial_number or "",
            fsp=ctx.fsp,
            olt_ont_id=ctx.olt_ont_id,
            line_profile_id=effective_values.get("authorization_line_profile_id"),
            service_profile_id=effective_values.get("authorization_service_profile_id"),
            service_ports=tuple(service_ports),
            management=management,
            tr069=tr069,
            internet_config_ip_index=effective_values.get("internet_config_ip_index"),
            wan_config_profile_id=effective_values.get("wan_config_profile_id"),
        ),
        "",
    )

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

        management = None
        tr069_profile_id = None
        internet_config_ip_indices: tuple[int, ...] = ()
        wan_config_profiles: dict[int, int] = {}

        # Read ONT-scoped config in interface mode. These display commands vary
        # across Huawei releases, so failures are treated as "unknown" and the
        # reconciler keeps the existing conservative write behavior.
        config_prompt = r"[#)]\s*$"
        frame_slot = f"{parts[0]}/{parts[1]}"
        port_num = parts[2]
        _run_huawei_cmd(channel, "config", prompt=config_prompt)
        _run_huawei_cmd(channel, f"interface gpon {frame_slot}", prompt=config_prompt)

        iphost_output = _safe_display_command(
            channel,
            f"display ont ipconfig {port_num} {olt_ont_id}",
            prompt=config_prompt,
        )
        if iphost_output:
            management = _parse_actual_management_config(iphost_output)

        tr069_output = _safe_display_command(
            channel,
            f"display ont tr069-server-config {port_num} {olt_ont_id}",
            prompt=config_prompt,
        )
        if tr069_output:
            tr069_profile_id = _parse_tr069_profile_id(tr069_output)

        internet_output = _safe_display_command(
            channel,
            f"display ont internet-config {port_num} {olt_ont_id}",
            prompt=config_prompt,
        )
        if internet_output:
            internet_config_ip_indices = _parse_ip_indices(internet_output)

        wan_output = _safe_display_command(
            channel,
            f"display ont wan-config {port_num} {olt_ont_id}",
            prompt=config_prompt,
        )
        if wan_output:
            wan_config_profiles = _parse_wan_config_profiles(wan_output)

        return (
            ActualOntState(
                is_authorized=is_authorized,
                olt_ont_id=olt_ont_id,
                service_ports=actual_ports,
                management=management,
                tr069_profile_id=tr069_profile_id,
                internet_config_ip_indices=internet_config_ip_indices,
                wan_config_profiles=wan_config_profiles,
            ),
            "",
        )
    except Exception as exc:
        logger.error("Error reading ONT state from OLT %s: %s", olt.name, exc)
        return None, f"Error reading state: {exc}"
    finally:
        transport.close()
