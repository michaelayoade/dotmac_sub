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

if TYPE_CHECKING:
    from app.models.network import OLTDevice

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
class OltOperationResult:
    """Result of an OLT write operation."""

    success: bool
    message: str
    data: dict[str, object] = field(default_factory=dict)

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
    can_configure_iphost: bool = False
    can_bind_tr069: bool = False
    can_create_service_port: bool = False
    can_reboot_ont: bool = False
    can_factory_reset: bool = False


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
    def olt(self) -> "OLTDevice":
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

    # ========== ONT Operations ==========

    def reboot_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Reboot an ONT via OMCI."""
        ...

    def factory_reset_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        """Factory reset an ONT via OMCI."""
        ...


# ============================================================================
# Base Implementation
# ============================================================================


class BaseProtocolAdapter(ABC):
    """Base class with common functionality for protocol adapters."""

    def __init__(self, olt: "OLTDevice"):
        self._olt = olt

    @property
    def olt(self) -> "OLTDevice":
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

    def reboot_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._not_supported("reboot_ont")

    def factory_reset_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._not_supported("factory_reset_ont")


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
            can_configure_iphost=ssh_available,
            can_bind_tr069=ssh_available,
            can_create_service_port=ssh_available,
            can_reboot_ont=ssh_available,
            can_factory_reset=ssh_available,
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
        from app.services.network.olt_ssh_ont.lifecycle import (
            authorize_ont as ssh_authorize,
        )

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
        from app.services.network.olt_ssh_ont.lifecycle import deauthorize_ont

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
            ok, message = configure_ont_iphost(
                self._olt,
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
        from app.services.network.olt_ssh_ont.tr069 import bind_tr069_server_profile

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
        from app.services.network.olt_ssh_service_ports import create_single_service_port

        try:
            ok, message, created_index = create_single_service_port(
                self._olt,
                fsp,
                ont_id,
                gem_index,
                vlan_id,
                user_vlan=user_vlan,
                tag_transform=tag_transform,
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
        olt: "OLTDevice",
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
            can_configure_iphost=nc_caps.can_configure_iphost or ssh_caps.can_configure_iphost,
            can_bind_tr069=nc_caps.can_bind_tr069 or ssh_caps.can_bind_tr069,
            can_create_service_port=nc_caps.can_create_service_port or ssh_caps.can_create_service_port,
            can_reboot_ont=nc_caps.can_reboot_ont or ssh_caps.can_reboot_ont,
            can_factory_reset=nc_caps.can_factory_reset or ssh_caps.can_factory_reset,
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

    def reboot_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._ssh.reboot_ont(fsp, ont_id)

    def factory_reset_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        return self._ssh.factory_reset_ont(fsp, ont_id)


# ============================================================================
# Factory
# ============================================================================


def get_protocol_adapter(
    olt: "OLTDevice",
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


def get_ssh_adapter(olt: "OLTDevice") -> SshProtocolAdapter:
    """Get SSH adapter directly (convenience function)."""
    return SshProtocolAdapter(olt)


def get_netconf_adapter(olt: "OLTDevice") -> NetconfProtocolAdapter:
    """Get NETCONF adapter directly (convenience function)."""
    return NetconfProtocolAdapter(olt)
