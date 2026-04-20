"""OLT Protocol Adapter pattern for SSH/NETCONF/REST abstraction.

Provides a clean abstraction for OLT write operations, allowing different
communication protocols (SSH, NETCONF, REST API) to be used interchangeably.

The adapter automatically selects the best available protocol based on:
1. OLT configuration (netconf_enabled, api_enabled)
2. OLT capabilities (GPON YANG support)
3. Operation support (not all operations available on all protocols)

Usage:
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    adapter = get_protocol_adapter(olt)
    result = adapter.authorize_ont(fsp="0/1/0", serial="HWTC12345678", ...)

    # Or with explicit protocol selection
    adapter = get_protocol_adapter(olt, protocol="ssh")
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.services.adapters.base import AdapterResult

if TYPE_CHECKING:
    from app.models.network import OLTDevice
    from app.services.network.olt_batched_auth import BatchedAuthorizationSpec
    from app.services.network.ont_provisioning.state import (
        DesiredOntState,
        ProvisioningDelta,
    )

logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes
# ============================================================================


class OltProtocol(str, Enum):
    """Available OLT communication protocols."""

    SSH = "ssh"
    NETCONF = "netconf"
    REST = "rest"
    AUTO = "auto"  # Automatic selection


@dataclass
class OltOperationResult(AdapterResult):
    """Result of an OLT write operation."""

    # For authorize_ont: the assigned ONT ID
    ont_id: int | None = None

    # Protocol that was used
    protocol_used: OltProtocol | None = None

    # If fallback occurred, the reason
    fallback_reason: str | None = None


@dataclass
class ProtocolCapabilities:
    """Capabilities of a protocol for a specific OLT."""

    protocol: OltProtocol
    available: bool
    reason: str = ""

    # Specific operation support
    can_authorize: bool = False
    can_deauthorize: bool = False
    can_update_ont_profiles: bool = False
    can_find_ont_by_serial: bool = False
    can_configure_iphost: bool = False
    can_bind_tr069: bool = False
    can_create_service_port: bool = False
    can_reboot_ont: bool = False
    can_factory_reset: bool = False
    can_execute_authorization_batch: bool = False
    can_execute_provisioning_delta: bool = False

    # Extended configuration operations
    can_configure_internet_config: bool = False
    can_configure_wan_config: bool = False
    can_configure_pppoe: bool = False
    can_configure_port_native_vlan: bool = False
    can_clear_configs: bool = False

    # Read operations
    can_get_service_ports: bool = False
    can_get_autofind_onts: bool = False
    can_get_profiles: bool = False
    can_create_tr069_profile: bool = False
    can_diagnose_service_ports: bool = False
    can_fetch_running_config: bool = False


# ============================================================================
# Protocol Definition
# ============================================================================


@runtime_checkable
class OltProtocolAdapter(Protocol):
    """Protocol for OLT write operations.

    Implementations provide operations via specific protocols (SSH, NETCONF, etc.).
    """

    @property
    def protocol(self) -> OltProtocol:
        """The protocol this adapter uses."""
        ...

    @property
    def olt(self) -> OLTDevice:
        """The OLT device this adapter operates on."""
        ...

    def get_capabilities(self) -> ProtocolCapabilities:
        """Get capabilities of this protocol for the OLT."""
        ...

    # ========== ONT Lifecycle ==========

    def authorize_ont(
        self,
        fsp: str,
        serial_number: str,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
        description: str = "",
    ) -> OltOperationResult:
        """Authorize an ONT on the OLT.

        Args:
            fsp: Frame/Slot/Port (e.g., "0/1/0")
            serial_number: ONT serial number
            line_profile_id: Line profile ID
            service_profile_id: Service profile ID
            description: Optional description

        Returns:
            OltOperationResult with ont_id if successful
        """
        ...

    def deauthorize_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Remove/deauthorize an ONT from the OLT."""
        ...

    def find_ont_by_serial(self, serial_number: str) -> OltOperationResult:
        """Find an existing ONT registration by serial number."""
        ...

    def update_ont_profiles(
        self,
        fsp: str,
        ont_id: int,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
    ) -> OltOperationResult:
        """Update an existing ONT's line and/or service profile."""
        ...

    # ========== ONT Configuration ==========

    def configure_iphost(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
        mode: str = "dhcp",
        vlan: int,
        priority: int | None = None,
        ip_address: str | None = None,
        subnet_mask: str | None = None,
        gateway: str | None = None,
    ) -> OltOperationResult:
        """Configure ONT management IP (IPHOST)."""
        ...

    def bind_tr069_profile(
        self,
        fsp: str,
        ont_id: int,
        *,
        profile_id: int,
    ) -> OltOperationResult:
        """Bind TR-069 server profile to ONT."""
        ...

    # ========== Service Ports ==========

    def create_service_port(
        self,
        fsp: str,
        ont_id: int,
        *,
        gem_index: int,
        vlan_id: int,
        user_vlan: int | str | None = None,
        tag_transform: str = "translate",
        port_index: int | None = None,
    ) -> OltOperationResult:
        """Create a service port for the ONT."""
        ...

    def delete_service_port(self, port_index: int) -> OltOperationResult:
        """Delete a service port by index."""
        ...

    # ========== Batched Write Operations ==========

    def execute_authorization_batch(
        self,
        spec: BatchedAuthorizationSpec,
    ) -> OltOperationResult:
        """Authorize an ONT and apply related OLT config in one protocol session."""
        ...

    def execute_provisioning_delta(
        self,
        delta: ProvisioningDelta,
        desired: DesiredOntState,
        *,
        dry_run: bool = False,
    ) -> OltOperationResult:
        """Execute a reconciled ONT provisioning delta with compensation tracking."""
        ...

    # ========== ONT Operations ==========

    def reboot_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Reboot an ONT via OMCI."""
        ...

    def factory_reset_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Factory reset an ONT via OMCI."""
        ...

    # ========== Extended Configuration Operations ==========

    def configure_internet_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        """Activate TCP stack on ONT management WAN via internet-config."""
        ...

    def configure_wan_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
        profile_id: int = 0,
    ) -> OltOperationResult:
        """Set route+NAT mode on ONT management WAN via wan-config."""
        ...

    def configure_pppoe(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int,
        vlan_id: int,
        username: str,
        password: str,
    ) -> OltOperationResult:
        """Configure PPPoE WAN via OMCI (OLT-side, not TR-069)."""
        ...

    def configure_port_native_vlan(
        self,
        fsp: str,
        ont_id: int,
        *,
        eth_port: int,
        vlan_id: int,
        priority: int = 0,
    ) -> OltOperationResult:
        """Set native VLAN on ONT Ethernet port for bridging mode."""
        ...

    # ========== Cleanup Operations ==========

    def clear_iphost_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        """Clear ONT IP configuration for a given IP index."""
        ...

    def clear_internet_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        """Clear ONT internet-config state."""
        ...

    def clear_wan_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        """Clear ONT wan-config state."""
        ...

    def unbind_tr069_profile(
        self,
        fsp: str,
        ont_id: int,
    ) -> OltOperationResult:
        """Remove TR-069 server profile binding from ONT."""
        ...

    # ========== Read Operations ==========

    def get_service_ports(
        self,
        fsp: str,
    ) -> OltOperationResult:
        """Get all service-ports on a PON port.

        Returns:
            OltOperationResult with data["service_ports"] containing list of entries.
        """
        ...

    def get_service_ports_for_ont(
        self,
        fsp: str,
        ont_id: int,
    ) -> OltOperationResult:
        """Get service-ports for a specific ONT.

        Returns:
            OltOperationResult with data["service_ports"] containing list of entries.
        """
        ...

    def get_autofind_onts(self) -> OltOperationResult:
        """Get unregistered ONTs from autofind table.

        Returns:
            OltOperationResult with data["autofind_entries"] containing list of entries.
        """
        ...

    def get_line_profiles(self) -> OltOperationResult:
        """Get line profiles from the OLT."""
        ...

    def get_service_profiles(self) -> OltOperationResult:
        """Get service profiles from the OLT."""
        ...

    def get_tr069_profiles(self) -> OltOperationResult:
        """Get TR-069 server profiles from the OLT."""
        ...

    def create_tr069_profile(
        self,
        *,
        profile_name: str,
        acs_url: str,
        username: str,
        password: str,
        inform_interval: int,
    ) -> OltOperationResult:
        """Create a TR-069 server profile on the OLT."""
        ...

    def diagnose_service_ports(
        self,
        fsp: str,
        ont_id: int,
    ) -> OltOperationResult:
        """Run diagnostics to troubleshoot service port state issues.

        Returns:
            OltOperationResult with data["diagnostics"] containing diagnostic info.
        """
        ...

    def fetch_running_config(self) -> OltOperationResult:
        """Fetch the full OLT running configuration.

        Returns:
            OltOperationResult with data["config_text"] containing the config text.
        """
        ...


# ============================================================================
# Base Implementation
# ============================================================================


class BaseProtocolAdapter(ABC):
    """Base class with common functionality for protocol adapters."""

    def __init__(self, olt: OLTDevice):
        self._olt = olt

    @property
    @abstractmethod
    def protocol(self) -> OltProtocol:
        """The protocol this adapter uses."""
        ...

    @property
    def olt(self) -> OLTDevice:
        return self._olt

    def _not_supported(self, operation: str) -> OltOperationResult:
        """Return a 'not supported' result for an operation."""
        return OltOperationResult(
            success=False,
            message=f"{operation} not supported via {self.protocol.value}",
            protocol_used=self.protocol,
        )

    # Default implementations that return 'not supported'

    def deauthorize_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._not_supported("deauthorize_ont")

    def find_ont_by_serial(self, serial_number: str) -> OltOperationResult:
        return self._not_supported("find_ont_by_serial")

    def update_ont_profiles(
        self,
        fsp: str,
        ont_id: int,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
    ) -> OltOperationResult:
        return self._not_supported("update_ont_profiles")

    def configure_iphost(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
        mode: str = "dhcp",
        vlan: int,
        priority: int | None = None,
        ip_address: str | None = None,
        subnet_mask: str | None = None,
        gateway: str | None = None,
    ) -> OltOperationResult:
        return self._not_supported("configure_iphost")

    def bind_tr069_profile(
        self,
        fsp: str,
        ont_id: int,
        *,
        profile_id: int,
    ) -> OltOperationResult:
        return self._not_supported("bind_tr069_profile")

    def create_service_port(
        self,
        fsp: str,
        ont_id: int,
        *,
        gem_index: int,
        vlan_id: int,
        user_vlan: int | str | None = None,
        tag_transform: str = "translate",
        port_index: int | None = None,
    ) -> OltOperationResult:
        return self._not_supported("create_service_port")

    def delete_service_port(self, port_index: int) -> OltOperationResult:
        return self._not_supported("delete_service_port")

    def execute_authorization_batch(
        self,
        spec: BatchedAuthorizationSpec,
    ) -> OltOperationResult:
        return self._not_supported("execute_authorization_batch")

    def execute_provisioning_delta(
        self,
        delta: ProvisioningDelta,
        desired: DesiredOntState,
        *,
        dry_run: bool = False,
    ) -> OltOperationResult:
        return self._not_supported("execute_provisioning_delta")

    def reboot_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._not_supported("reboot_ont")

    def factory_reset_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._not_supported("factory_reset_ont")

    # Extended configuration operations

    def configure_internet_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        return self._not_supported("configure_internet_config")

    def configure_wan_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
        profile_id: int = 0,
    ) -> OltOperationResult:
        return self._not_supported("configure_wan_config")

    def configure_pppoe(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int,
        vlan_id: int,
        username: str,
        password: str,
    ) -> OltOperationResult:
        return self._not_supported("configure_pppoe")

    def configure_port_native_vlan(
        self,
        fsp: str,
        ont_id: int,
        *,
        eth_port: int,
        vlan_id: int,
        priority: int = 0,
    ) -> OltOperationResult:
        return self._not_supported("configure_port_native_vlan")

    # Cleanup operations

    def clear_iphost_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        return self._not_supported("clear_iphost_config")

    def clear_internet_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        return self._not_supported("clear_internet_config")

    def clear_wan_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        return self._not_supported("clear_wan_config")

    def unbind_tr069_profile(
        self,
        fsp: str,
        ont_id: int,
    ) -> OltOperationResult:
        return self._not_supported("unbind_tr069_profile")

    # Read operations

    def get_service_ports(self, fsp: str) -> OltOperationResult:
        return self._not_supported("get_service_ports")

    def get_service_ports_for_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._not_supported("get_service_ports_for_ont")

    def get_autofind_onts(self) -> OltOperationResult:
        return self._not_supported("get_autofind_onts")

    def get_line_profiles(self) -> OltOperationResult:
        return self._not_supported("get_line_profiles")

    def get_service_profiles(self) -> OltOperationResult:
        return self._not_supported("get_service_profiles")

    def get_tr069_profiles(self) -> OltOperationResult:
        return self._not_supported("get_tr069_profiles")

    def create_tr069_profile(
        self,
        *,
        profile_name: str,
        acs_url: str,
        username: str,
        password: str,
        inform_interval: int,
    ) -> OltOperationResult:
        return self._not_supported("create_tr069_profile")

    def diagnose_service_ports(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._not_supported("diagnose_service_ports")


# ============================================================================
# SSH Protocol Adapter
# ============================================================================


class SshProtocolAdapter(BaseProtocolAdapter):
    """SSH/CLI protocol adapter for OLT operations."""

    @property
    def protocol(self) -> OltProtocol:
        return OltProtocol.SSH

    def get_capabilities(self) -> ProtocolCapabilities:
        """SSH supports all operations for Huawei OLTs."""
        from app.services.network.olt_vendor_adapters import get_olt_adapter

        adapter = get_olt_adapter(self._olt)
        ssh_available = adapter.supports_ssh()

        return ProtocolCapabilities(
            protocol=OltProtocol.SSH,
            available=ssh_available,
            reason="" if ssh_available else f"{adapter.vendor_name} SSH not implemented",
            can_authorize=ssh_available,
            can_deauthorize=ssh_available,
            can_update_ont_profiles=ssh_available,
            can_find_ont_by_serial=ssh_available,
            can_configure_iphost=ssh_available,
            can_bind_tr069=ssh_available,
            can_create_service_port=ssh_available,
            can_reboot_ont=ssh_available,
            can_factory_reset=ssh_available,
            can_execute_authorization_batch=ssh_available,
            can_execute_provisioning_delta=ssh_available,
            # Extended configuration operations
            can_configure_internet_config=ssh_available,
            can_configure_wan_config=ssh_available,
            can_configure_pppoe=ssh_available,
            can_configure_port_native_vlan=ssh_available,
            can_clear_configs=ssh_available,
            # Read operations
            can_get_service_ports=ssh_available,
            can_get_autofind_onts=ssh_available,
            can_get_profiles=ssh_available,
            can_create_tr069_profile=ssh_available,
            can_diagnose_service_ports=ssh_available,
            can_fetch_running_config=ssh_available,
        )

    def fetch_running_config(self) -> OltOperationResult:
        """Fetch full running config via SSH CLI."""
        from app.services.network.olt_ssh import fetch_running_config_ssh

        try:
            ok, message, config_text = fetch_running_config_ssh(self._olt)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"config_text": config_text} if config_text else {},
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH fetch_running_config failed")
            return OltOperationResult(
                success=False,
                message=f"SSH running-config fetch failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def authorize_ont(
        self,
        fsp: str,
        serial_number: str,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
        description: str = "",
    ) -> OltOperationResult:
        """Authorize ONT via SSH CLI."""
        from app.services.network.olt_ssh import authorize_ont as ssh_authorize

        try:
            ok, message, ont_id = ssh_authorize(
                self._olt,
                fsp,
                serial_number,
                line_profile_id=line_profile_id,
                service_profile_id=service_profile_id,
            )
            return OltOperationResult(
                success=ok,
                message=message,
                ont_id=ont_id,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH authorize_ont failed")
            return OltOperationResult(
                success=False,
                message=f"SSH authorization failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def deauthorize_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Deauthorize ONT via SSH CLI."""
        from app.services.network.olt_ssh_ont import deauthorize_ont

        try:
            ok, message = deauthorize_ont(self._olt, fsp, ont_id)
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH deauthorize_ont failed")
            return OltOperationResult(
                success=False,
                message=f"SSH deauthorization failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def find_ont_by_serial(self, serial_number: str) -> OltOperationResult:
        """Find ONT registration by serial via SSH CLI."""
        from app.services.network.olt_ssh_ont import find_ont_by_serial

        try:
            ok, message, entry = find_ont_by_serial(self._olt, serial_number)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"registration": entry} if entry is not None else {},
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH find_ont_by_serial failed")
            return OltOperationResult(
                success=False,
                message=f"SSH ONT lookup failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def update_ont_profiles(
        self,
        fsp: str,
        ont_id: int,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
    ) -> OltOperationResult:
        """Update ONT profile binding via SSH CLI."""
        from app.services.network.olt_ssh import update_ont_profiles

        try:
            ok, message = update_ont_profiles(
                self._olt,
                fsp,
                ont_id,
                line_profile_id=line_profile_id,
                service_profile_id=service_profile_id,
            )
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH update_ont_profiles failed")
            return OltOperationResult(
                success=False,
                message=f"SSH ONT profile update failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def configure_iphost(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
        mode: str = "dhcp",
        vlan: int,
        priority: int | None = None,
        ip_address: str | None = None,
        subnet_mask: str | None = None,
        gateway: str | None = None,
    ) -> OltOperationResult:
        """Configure ONT IPHOST via SSH CLI."""
        from app.services.network.olt_ssh_ont.iphost import configure_ont_iphost

        try:
            # Map adapter params to underlying SSH function params
            ok, message = configure_ont_iphost(
                self._olt,
                fsp,
                ont_id,
                vlan_id=vlan,
                ip_mode=mode,
                priority=priority,
                ip_address=ip_address,
                subnet=subnet_mask,
                gateway=gateway,
            )
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH configure_iphost failed")
            return OltOperationResult(
                success=False,
                message=f"SSH IPHOST configuration failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def bind_tr069_profile(
        self,
        fsp: str,
        ont_id: int,
        *,
        profile_id: int,
    ) -> OltOperationResult:
        """Bind TR-069 profile via SSH CLI."""
        from app.services.network.olt_ssh_ont import bind_tr069_server_profile

        try:
            ok, message = bind_tr069_server_profile(
                self._olt, fsp, ont_id, profile_id=profile_id
            )
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH bind_tr069_profile failed")
            return OltOperationResult(
                success=False,
                message=f"SSH TR-069 binding failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def create_service_port(
        self,
        fsp: str,
        ont_id: int,
        *,
        gem_index: int,
        vlan_id: int,
        user_vlan: int | str | None = None,
        tag_transform: str = "translate",
        port_index: int | None = None,
    ) -> OltOperationResult:
        """Create service port via SSH CLI."""
        from app.services.network.olt_ssh_service_ports import (
            create_single_service_port,
        )

        try:
            ok, message, created_index = create_single_service_port(
                self._olt,
                fsp,
                ont_id,
                gem_index,
                vlan_id,
                user_vlan=user_vlan,
                tag_transform=tag_transform,
                port_index=port_index,
            )
            return OltOperationResult(
                success=ok,
                message=message,
                data={"port_index": created_index} if created_index else {},
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH create_service_port failed")
            return OltOperationResult(
                success=False,
                message=f"SSH service port creation failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def delete_service_port(self, port_index: int) -> OltOperationResult:
        """Delete service port via SSH CLI."""
        from app.services.network.olt_ssh_service_ports import (
            delete_service_port as ssh_delete,
        )

        try:
            ok, message = ssh_delete(self._olt, port_index)
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH delete_service_port failed")
            return OltOperationResult(
                success=False,
                message=f"SSH service port deletion failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def execute_authorization_batch(
        self,
        spec: BatchedAuthorizationSpec,
    ) -> OltOperationResult:
        """Execute batched ONT authorization via one SSH session."""
        from app.services.network.olt_batched_auth import execute_batched_authorization

        try:
            result = execute_batched_authorization(self._olt, spec)
            return OltOperationResult(
                success=result.success,
                message=result.error_message or "Batched authorization complete",
                ont_id=result.ont_id,
                data={
                    "batch_result": result,
                    "service_port_indices": result.service_port_indices,
                    "steps_completed": result.steps_completed,
                    "steps_failed": result.steps_failed,
                    "raw_output": result.raw_output,
                },
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH execute_authorization_batch failed")
            return OltOperationResult(
                success=False,
                message=f"SSH batched authorization failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def execute_provisioning_delta(
        self,
        delta: ProvisioningDelta,
        desired: DesiredOntState,
        *,
        dry_run: bool = False,
    ) -> OltOperationResult:
        """Execute reconciled provisioning delta via the SSH batch engine."""
        from app.services.network.ont_provisioning.executor import execute_delta

        try:
            result = execute_delta(self._olt, delta, desired, dry_run=dry_run)
            return OltOperationResult(
                success=result.success,
                message=result.message,
                data={
                    "execution_result": result,
                    "steps_completed": result.steps_completed,
                    "steps_failed": result.steps_failed,
                    "errors": result.errors,
                    "compensation_log": result.compensation_log,
                },
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH execute_provisioning_delta failed")
            return OltOperationResult(
                success=False,
                message=f"SSH provisioning delta execution failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def reboot_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Reboot ONT via SSH/OMCI."""
        from app.services.network.olt_ssh_ont.lifecycle import reboot_ont_omci

        try:
            ok, message = reboot_ont_omci(self._olt, fsp, ont_id)
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH reboot_ont failed")
            return OltOperationResult(
                success=False,
                message=f"SSH ONT reboot failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def factory_reset_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Factory reset ONT via SSH/OMCI."""
        from app.services.network.olt_ssh_ont.lifecycle import factory_reset_ont_omci

        try:
            ok, message = factory_reset_ont_omci(self._olt, fsp, ont_id)
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH factory_reset_ont failed")
            return OltOperationResult(
                success=False,
                message=f"SSH ONT factory reset failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    # ========== Extended Configuration Operations ==========

    def configure_internet_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        """Activate TCP stack on ONT management WAN via internet-config."""
        from app.services.network.olt_ssh_ont.omci_config import (
            configure_ont_internet_config,
        )

        try:
            ok, message = configure_ont_internet_config(
                self._olt, fsp, ont_id, ip_index=ip_index
            )
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH configure_internet_config failed")
            return OltOperationResult(
                success=False,
                message=f"SSH internet-config failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def configure_wan_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
        profile_id: int = 0,
    ) -> OltOperationResult:
        """Set route+NAT mode on ONT management WAN via wan-config."""
        from app.services.network.olt_ssh_ont.omci_config import (
            configure_ont_wan_config,
        )

        try:
            ok, message = configure_ont_wan_config(
                self._olt, fsp, ont_id, ip_index=ip_index, profile_id=profile_id
            )
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH configure_wan_config failed")
            return OltOperationResult(
                success=False,
                message=f"SSH wan-config failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def configure_pppoe(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int,
        vlan_id: int,
        username: str,
        password: str,
    ) -> OltOperationResult:
        """Configure PPPoE WAN via OMCI (OLT-side, not TR-069)."""
        from app.services.network.olt_ssh_ont.omci_config import (
            configure_ont_pppoe_omci,
        )

        try:
            ok, message = configure_ont_pppoe_omci(
                self._olt,
                fsp,
                ont_id,
                ip_index=ip_index,
                vlan_id=vlan_id,
                username=username,
                password=password,
            )
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH configure_pppoe failed")
            return OltOperationResult(
                success=False,
                message=f"SSH PPPoE configuration failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def configure_port_native_vlan(
        self,
        fsp: str,
        ont_id: int,
        *,
        eth_port: int,
        vlan_id: int,
        priority: int = 0,
    ) -> OltOperationResult:
        """Set native VLAN on ONT Ethernet port for bridging mode."""
        from app.services.network.olt_ssh_ont.omci_config import (
            configure_ont_port_native_vlan,
        )

        try:
            ok, message = configure_ont_port_native_vlan(
                self._olt,
                fsp,
                ont_id,
                eth_port=eth_port,
                vlan_id=vlan_id,
                priority=priority,
            )
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH configure_port_native_vlan failed")
            return OltOperationResult(
                success=False,
                message=f"SSH port native VLAN configuration failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    # ========== Cleanup Operations ==========

    def clear_iphost_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        """Clear ONT IP configuration for a given IP index."""
        from app.services.network.olt_ssh_ont.iphost import clear_ont_ipconfig

        try:
            ok, message = clear_ont_ipconfig(self._olt, fsp, ont_id, ip_index=ip_index)
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH clear_iphost_config failed")
            return OltOperationResult(
                success=False,
                message=f"SSH clear iphost config failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def clear_internet_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        """Clear ONT internet-config state."""
        from app.services.network.olt_ssh_ont.omci_config import (
            clear_ont_internet_config,
        )

        try:
            ok, message = clear_ont_internet_config(
                self._olt, fsp, ont_id, ip_index=ip_index
            )
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH clear_internet_config failed")
            return OltOperationResult(
                success=False,
                message=f"SSH clear internet config failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def clear_wan_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        """Clear ONT wan-config state."""
        from app.services.network.olt_ssh_ont.omci_config import (
            clear_ont_wan_config as ssh_clear,
        )

        try:
            ok, message = ssh_clear(self._olt, fsp, ont_id, ip_index=ip_index)
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH clear_wan_config failed")
            return OltOperationResult(
                success=False,
                message=f"SSH clear WAN config failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def unbind_tr069_profile(
        self,
        fsp: str,
        ont_id: int,
    ) -> OltOperationResult:
        """Remove TR-069 server profile binding from ONT."""
        from app.services.network.olt_ssh_ont.tr069 import unbind_tr069_server_profile

        try:
            ok, message = unbind_tr069_server_profile(self._olt, fsp, ont_id)
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH unbind_tr069_profile failed")
            return OltOperationResult(
                success=False,
                message=f"SSH unbind TR-069 profile failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    # ========== Read Operations ==========

    def get_service_ports(self, fsp: str) -> OltOperationResult:
        """Get all service-ports on a PON port."""
        from app.services.network import olt_ssh as core

        try:
            ok, message, entries = core.get_service_ports(self._olt, fsp)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"service_ports": entries},
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH get_service_ports failed")
            return OltOperationResult(
                success=False,
                message=f"SSH get service ports failed: {exc}",
                data={"service_ports": []},
                protocol_used=OltProtocol.SSH,
            )

    def get_service_ports_for_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Get service-ports for a specific ONT."""
        from app.services.network.olt_ssh_service_ports import get_service_ports_for_ont

        try:
            ok, message, entries = get_service_ports_for_ont(self._olt, fsp, ont_id)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"service_ports": entries},
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH get_service_ports_for_ont failed")
            return OltOperationResult(
                success=False,
                message=f"SSH get service ports for ONT failed: {exc}",
                data={"service_ports": []},
                protocol_used=OltProtocol.SSH,
            )

    def get_autofind_onts(self) -> OltOperationResult:
        """Get unregistered ONTs from autofind table."""
        from app.services.network import olt_ssh as core

        try:
            ok, message, entries = core.get_autofind_onts(self._olt)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"autofind_entries": entries},
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH get_autofind_onts failed")
            return OltOperationResult(
                success=False,
                message=f"SSH get autofind ONTs failed: {exc}",
                data={"autofind_entries": []},
                protocol_used=OltProtocol.SSH,
            )

    def get_line_profiles(self) -> OltOperationResult:
        """Get line profiles via SSH CLI."""
        from app.services.network.olt_ssh_profiles import get_line_profiles

        try:
            ok, message, entries = get_line_profiles(self._olt)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"profiles": entries},
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH get_line_profiles failed")
            return OltOperationResult(
                success=False,
                message=f"SSH get line profiles failed: {exc}",
                data={"profiles": []},
                protocol_used=OltProtocol.SSH,
            )

    def get_service_profiles(self) -> OltOperationResult:
        """Get service profiles via SSH CLI."""
        from app.services.network.olt_ssh_profiles import get_service_profiles

        try:
            ok, message, entries = get_service_profiles(self._olt)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"profiles": entries},
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH get_service_profiles failed")
            return OltOperationResult(
                success=False,
                message=f"SSH get service profiles failed: {exc}",
                data={"profiles": []},
                protocol_used=OltProtocol.SSH,
            )

    def get_tr069_profiles(self) -> OltOperationResult:
        """Get TR-069 server profiles via SSH CLI."""
        from app.services.network.olt_ssh_profiles import get_tr069_server_profiles

        try:
            ok, message, entries = get_tr069_server_profiles(self._olt)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"profiles": entries},
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH get_tr069_profiles failed")
            return OltOperationResult(
                success=False,
                message=f"SSH get TR-069 profiles failed: {exc}",
                data={"profiles": []},
                protocol_used=OltProtocol.SSH,
            )

    def create_tr069_profile(
        self,
        *,
        profile_name: str,
        acs_url: str,
        username: str,
        password: str,
        inform_interval: int,
    ) -> OltOperationResult:
        """Create a TR-069 server profile via SSH CLI."""
        from app.services.network.olt_ssh import create_tr069_server_profile

        try:
            ok, message = create_tr069_server_profile(
                self._olt,
                profile_name=profile_name,
                acs_url=acs_url,
                username=username,
                password=password,
                inform_interval=inform_interval,
            )
            return OltOperationResult(
                success=ok,
                message=message,
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH create_tr069_profile failed")
            return OltOperationResult(
                success=False,
                message=f"SSH create TR-069 profile failed: {exc}",
                protocol_used=OltProtocol.SSH,
            )

    def diagnose_service_ports(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Run diagnostics to troubleshoot service port state issues."""
        from app.services.network.olt_ssh_ont.diagnostics import (
            diagnose_service_ports as ssh_diagnose,
        )

        try:
            ok, message, diagnostics = ssh_diagnose(self._olt, fsp, ont_id)
            return OltOperationResult(
                success=ok,
                message=message,
                data={"diagnostics": diagnostics},
                protocol_used=OltProtocol.SSH,
            )
        except Exception as exc:
            logger.exception("SSH diagnose_service_ports failed")
            return OltOperationResult(
                success=False,
                message=f"SSH diagnose service ports failed: {exc}",
                data={"diagnostics": None},
                protocol_used=OltProtocol.SSH,
            )


# ============================================================================
# NETCONF Protocol Adapter
# ============================================================================


class NetconfProtocolAdapter(BaseProtocolAdapter):
    """NETCONF protocol adapter for OLT operations."""

    @property
    def protocol(self) -> OltProtocol:
        return OltProtocol.NETCONF

    def get_capabilities(self) -> ProtocolCapabilities:
        """Check NETCONF capabilities for the OLT."""
        if not self._olt.netconf_enabled:
            return ProtocolCapabilities(
                protocol=OltProtocol.NETCONF,
                available=False,
                reason="NETCONF not enabled on OLT",
            )

        from app.services.network.olt_netconf_ont import can_authorize_via_netconf

        can_use, reason = can_authorize_via_netconf(self._olt)

        return ProtocolCapabilities(
            protocol=OltProtocol.NETCONF,
            available=can_use,
            reason=reason,
            can_authorize=can_use,
            # NETCONF currently only supports authorization
            can_deauthorize=False,
            can_update_ont_profiles=False,
            can_find_ont_by_serial=False,
            can_configure_iphost=False,
            can_bind_tr069=False,
            can_create_service_port=False,
            can_reboot_ont=False,
            can_factory_reset=False,
        )

    def authorize_ont(
        self,
        fsp: str,
        serial_number: str,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
        description: str = "",
    ) -> OltOperationResult:
        """Authorize ONT via NETCONF."""
        from app.services.network.olt_netconf_ont import authorize_ont as nc_authorize

        if line_profile_id is None or service_profile_id is None:
            return OltOperationResult(
                success=False,
                message="NETCONF authorization requires line_profile_id and service_profile_id",
                protocol_used=OltProtocol.NETCONF,
            )

        try:
            ok, message, ont_id = nc_authorize(
                self._olt,
                fsp,
                serial_number,
                line_profile_id=line_profile_id,
                service_profile_id=service_profile_id,
            )
            return OltOperationResult(
                success=ok,
                message=message,
                ont_id=ont_id,
                protocol_used=OltProtocol.NETCONF,
            )
        except Exception as exc:
            logger.exception("NETCONF authorize_ont failed")
            return OltOperationResult(
                success=False,
                message=f"NETCONF authorization failed: {exc}",
                protocol_used=OltProtocol.NETCONF,
            )


# ============================================================================
# Composite Adapter with Fallback
# ============================================================================


class CompositeProtocolAdapter(BaseProtocolAdapter):
    """Adapter that tries protocols in order with automatic fallback.

    Attempts NETCONF first (if enabled), falls back to SSH.
    """

    def __init__(
        self,
        olt: OLTDevice,
        *,
        prefer_netconf: bool = True,
    ):
        super().__init__(olt)
        self._prefer_netconf = prefer_netconf
        self._netconf = NetconfProtocolAdapter(olt)
        self._ssh = SshProtocolAdapter(olt)

    @property
    def protocol(self) -> OltProtocol:
        return OltProtocol.AUTO

    def get_capabilities(self) -> ProtocolCapabilities:
        """Composite capabilities from all protocols."""
        nc_caps = self._netconf.get_capabilities()
        ssh_caps = self._ssh.get_capabilities()

        return ProtocolCapabilities(
            protocol=OltProtocol.AUTO,
            available=nc_caps.available or ssh_caps.available,
            reason="",
            can_authorize=nc_caps.can_authorize or ssh_caps.can_authorize,
            can_deauthorize=nc_caps.can_deauthorize or ssh_caps.can_deauthorize,
            can_update_ont_profiles=(
                nc_caps.can_update_ont_profiles or ssh_caps.can_update_ont_profiles
            ),
            can_find_ont_by_serial=(
                nc_caps.can_find_ont_by_serial or ssh_caps.can_find_ont_by_serial
            ),
            can_configure_iphost=nc_caps.can_configure_iphost or ssh_caps.can_configure_iphost,
            can_bind_tr069=nc_caps.can_bind_tr069 or ssh_caps.can_bind_tr069,
            can_create_service_port=nc_caps.can_create_service_port or ssh_caps.can_create_service_port,
            can_reboot_ont=nc_caps.can_reboot_ont or ssh_caps.can_reboot_ont,
            can_factory_reset=nc_caps.can_factory_reset or ssh_caps.can_factory_reset,
            can_execute_authorization_batch=(
                nc_caps.can_execute_authorization_batch
                or ssh_caps.can_execute_authorization_batch
            ),
            can_execute_provisioning_delta=(
                nc_caps.can_execute_provisioning_delta
                or ssh_caps.can_execute_provisioning_delta
            ),
            # Extended configuration operations
            can_configure_internet_config=(
                nc_caps.can_configure_internet_config or ssh_caps.can_configure_internet_config
            ),
            can_configure_wan_config=(
                nc_caps.can_configure_wan_config or ssh_caps.can_configure_wan_config
            ),
            can_configure_pppoe=nc_caps.can_configure_pppoe or ssh_caps.can_configure_pppoe,
            can_configure_port_native_vlan=(
                nc_caps.can_configure_port_native_vlan or ssh_caps.can_configure_port_native_vlan
            ),
            can_clear_configs=nc_caps.can_clear_configs or ssh_caps.can_clear_configs,
            # Read operations
            can_get_service_ports=(
                nc_caps.can_get_service_ports or ssh_caps.can_get_service_ports
            ),
            can_get_autofind_onts=(
                nc_caps.can_get_autofind_onts or ssh_caps.can_get_autofind_onts
            ),
            can_get_profiles=nc_caps.can_get_profiles or ssh_caps.can_get_profiles,
            can_create_tr069_profile=(
                nc_caps.can_create_tr069_profile or ssh_caps.can_create_tr069_profile
            ),
            can_diagnose_service_ports=(
                nc_caps.can_diagnose_service_ports or ssh_caps.can_diagnose_service_ports
            ),
            can_fetch_running_config=(
                nc_caps.can_fetch_running_config or ssh_caps.can_fetch_running_config
            ),
        )

    def fetch_running_config(self) -> OltOperationResult:
        return self._ssh.fetch_running_config()

    def authorize_ont(
        self,
        fsp: str,
        serial_number: str,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
        description: str = "",
    ) -> OltOperationResult:
        """Authorize ONT, trying NETCONF first if enabled."""
        # Try NETCONF if preferred and available
        if self._prefer_netconf and self._olt.netconf_enabled:
            nc_caps = self._netconf.get_capabilities()
            if nc_caps.can_authorize:
                logger.info(
                    "Attempting ONT authorization via NETCONF: olt=%s fsp=%s serial=%s",
                    self._olt.name,
                    fsp,
                    serial_number,
                )
                result = self._netconf.authorize_ont(
                    fsp,
                    serial_number,
                    line_profile_id=line_profile_id,
                    service_profile_id=service_profile_id,
                    description=description,
                )
                if result.success:
                    return result

                # NETCONF failed, fall back to SSH
                logger.info(
                    "NETCONF authorization failed, falling back to SSH: %s",
                    result.message,
                )
                fallback_reason = result.message

                ssh_result = self._ssh.authorize_ont(
                    fsp,
                    serial_number,
                    line_profile_id=line_profile_id,
                    service_profile_id=service_profile_id,
                    description=description,
                )
                ssh_result.fallback_reason = f"NETCONF failed: {fallback_reason}"
                return ssh_result

        # Use SSH directly
        return self._ssh.authorize_ont(
            fsp,
            serial_number,
            line_profile_id=line_profile_id,
            service_profile_id=service_profile_id,
            description=description,
        )

    # For other operations, delegate to SSH (NETCONF doesn't support them yet)

    def deauthorize_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._ssh.deauthorize_ont(fsp, ont_id)

    def find_ont_by_serial(self, serial_number: str) -> OltOperationResult:
        return self._ssh.find_ont_by_serial(serial_number)

    def update_ont_profiles(
        self,
        fsp: str,
        ont_id: int,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
    ) -> OltOperationResult:
        return self._ssh.update_ont_profiles(
            fsp,
            ont_id,
            line_profile_id=line_profile_id,
            service_profile_id=service_profile_id,
        )

    def configure_iphost(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
        mode: str = "dhcp",
        vlan: int,
        priority: int | None = None,
        ip_address: str | None = None,
        subnet_mask: str | None = None,
        gateway: str | None = None,
    ) -> OltOperationResult:
        return self._ssh.configure_iphost(
            fsp,
            ont_id,
            ip_index=ip_index,
            mode=mode,
            vlan=vlan,
            priority=priority,
            ip_address=ip_address,
            subnet_mask=subnet_mask,
            gateway=gateway,
        )

    def bind_tr069_profile(
        self,
        fsp: str,
        ont_id: int,
        *,
        profile_id: int,
    ) -> OltOperationResult:
        return self._ssh.bind_tr069_profile(fsp, ont_id, profile_id=profile_id)

    def create_service_port(
        self,
        fsp: str,
        ont_id: int,
        *,
        gem_index: int,
        vlan_id: int,
        user_vlan: int | str | None = None,
        tag_transform: str = "translate",
        port_index: int | None = None,
    ) -> OltOperationResult:
        return self._ssh.create_service_port(
            fsp,
            ont_id,
            gem_index=gem_index,
            vlan_id=vlan_id,
            user_vlan=user_vlan,
            tag_transform=tag_transform,
            port_index=port_index,
        )

    def delete_service_port(self, port_index: int) -> OltOperationResult:
        return self._ssh.delete_service_port(port_index)

    def execute_authorization_batch(
        self,
        spec: BatchedAuthorizationSpec,
    ) -> OltOperationResult:
        return self._ssh.execute_authorization_batch(spec)

    def execute_provisioning_delta(
        self,
        delta: ProvisioningDelta,
        desired: DesiredOntState,
        *,
        dry_run: bool = False,
    ) -> OltOperationResult:
        return self._ssh.execute_provisioning_delta(
            delta,
            desired,
            dry_run=dry_run,
        )

    def reboot_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._ssh.reboot_ont(fsp, ont_id)

    def factory_reset_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._ssh.factory_reset_ont(fsp, ont_id)

    # Extended configuration operations - delegate to SSH

    def configure_internet_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        return self._ssh.configure_internet_config(fsp, ont_id, ip_index=ip_index)

    def configure_wan_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
        profile_id: int = 0,
    ) -> OltOperationResult:
        return self._ssh.configure_wan_config(
            fsp, ont_id, ip_index=ip_index, profile_id=profile_id
        )

    def configure_pppoe(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int,
        vlan_id: int,
        username: str,
        password: str,
    ) -> OltOperationResult:
        return self._ssh.configure_pppoe(
            fsp,
            ont_id,
            ip_index=ip_index,
            vlan_id=vlan_id,
            username=username,
            password=password,
        )

    def configure_port_native_vlan(
        self,
        fsp: str,
        ont_id: int,
        *,
        eth_port: int,
        vlan_id: int,
        priority: int = 0,
    ) -> OltOperationResult:
        return self._ssh.configure_port_native_vlan(
            fsp, ont_id, eth_port=eth_port, vlan_id=vlan_id, priority=priority
        )

    # Cleanup operations - delegate to SSH

    def clear_iphost_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        return self._ssh.clear_iphost_config(fsp, ont_id, ip_index=ip_index)

    def clear_internet_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        return self._ssh.clear_internet_config(fsp, ont_id, ip_index=ip_index)

    def clear_wan_config(
        self,
        fsp: str,
        ont_id: int,
        *,
        ip_index: int = 0,
    ) -> OltOperationResult:
        return self._ssh.clear_wan_config(fsp, ont_id, ip_index=ip_index)

    def unbind_tr069_profile(
        self,
        fsp: str,
        ont_id: int,
    ) -> OltOperationResult:
        return self._ssh.unbind_tr069_profile(fsp, ont_id)

    # Read operations - delegate to SSH

    def get_service_ports(self, fsp: str) -> OltOperationResult:
        return self._ssh.get_service_ports(fsp)

    def get_service_ports_for_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._ssh.get_service_ports_for_ont(fsp, ont_id)

    def get_autofind_onts(self) -> OltOperationResult:
        return self._ssh.get_autofind_onts()

    def get_line_profiles(self) -> OltOperationResult:
        return self._ssh.get_line_profiles()

    def get_service_profiles(self) -> OltOperationResult:
        return self._ssh.get_service_profiles()

    def get_tr069_profiles(self) -> OltOperationResult:
        return self._ssh.get_tr069_profiles()

    def create_tr069_profile(
        self,
        *,
        profile_name: str,
        acs_url: str,
        username: str,
        password: str,
        inform_interval: int,
    ) -> OltOperationResult:
        return self._ssh.create_tr069_profile(
            profile_name=profile_name,
            acs_url=acs_url,
            username=username,
            password=password,
            inform_interval=inform_interval,
        )

    def diagnose_service_ports(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._ssh.diagnose_service_ports(fsp, ont_id)


# ============================================================================
# Factory
# ============================================================================


def get_protocol_adapter(
    olt: OLTDevice,
    *,
    protocol: OltProtocol | str = OltProtocol.AUTO,
) -> OltProtocolAdapter:
    """Get the appropriate protocol adapter for an OLT.

    Args:
        olt: OLT device instance
        protocol: Specific protocol to use, or AUTO for automatic selection

    Returns:
        OltProtocolAdapter implementation

    Examples:
        # Automatic selection (NETCONF if available, else SSH)
        adapter = get_protocol_adapter(olt)

        # Force SSH
        adapter = get_protocol_adapter(olt, protocol="ssh")

        # Force NETCONF
        adapter = get_protocol_adapter(olt, protocol="netconf")
    """
    if isinstance(protocol, str):
        protocol = OltProtocol(protocol.lower())

    if protocol == OltProtocol.SSH:
        return SshProtocolAdapter(olt)
    elif protocol == OltProtocol.NETCONF:
        return NetconfProtocolAdapter(olt)
    elif protocol == OltProtocol.AUTO:
        return CompositeProtocolAdapter(olt, prefer_netconf=True)
    else:
        raise ValueError(f"Unknown protocol: {protocol}")


def get_ssh_adapter(olt: OLTDevice) -> SshProtocolAdapter:
    """Get SSH adapter directly (convenience function)."""
    return SshProtocolAdapter(olt)


def get_netconf_adapter(olt: OLTDevice) -> NetconfProtocolAdapter:
    """Get NETCONF adapter directly (convenience function)."""
    return NetconfProtocolAdapter(olt)
