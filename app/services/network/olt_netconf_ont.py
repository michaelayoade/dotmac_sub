"""NETCONF-based ONT authorization for Huawei OLTs.

This module provides NETCONF as an alternative to SSH/CLI for authorizing ONTs
on Huawei OLTs. NETCONF is preferred when enabled because it provides:
- Structured XML configuration (no CLI parsing)
- Transactional semantics
- Standard error reporting via RPC errors

The system automatically chooses NETCONF when enabled on the OLT and falls
back to SSH if NETCONF fails or lacks GPON support.
"""

from __future__ import annotations

import logging
import re

from ncclient.operations.rpc import RPCError

from app.models.network import OLTDevice
from app.services.network import olt_netconf

logger = logging.getLogger(__name__)

# Known Huawei GPON namespace patterns (discovered at runtime from OLT capabilities)
HUAWEI_GPON_NAMESPACES = [
    "urn:huawei:yang:huawei-gpon",
    "http://www.huawei.com/netconf/vrp/huawei-gpon",
    "urn:huawei:params:xml:ns:yang:huawei-gpon",
]

# Cache discovered namespaces per OLT (keyed by OLT ID)
_namespace_cache: dict[str, str | None] = {}


def can_authorize_via_netconf(olt: OLTDevice) -> tuple[bool, str]:
    """Check if OLT supports NETCONF-based ONT authorization.

    Args:
        olt: The OLT device to check.

    Returns:
        Tuple of (can_use_netconf, reason_message).
    """
    if not olt.netconf_enabled:
        return False, "NETCONF is not enabled on this OLT"

    # Test connection and check for GPON capabilities
    try:
        success, message, capabilities = olt_netconf.test_connection(olt)
        if not success:
            return False, f"NETCONF connection failed: {message}"

        # Check if OLT exposes GPON YANG module
        namespace = _find_gpon_namespace_in_capabilities(capabilities)
        if namespace is None:
            return False, "OLT does not expose GPON YANG capabilities"

        # Cache the discovered namespace
        _namespace_cache[str(olt.id)] = namespace
        return True, f"NETCONF available with GPON namespace: {namespace}"
    except Exception as exc:
        logger.warning(
            "Failed to check NETCONF capability on OLT %s: %s",
            olt.name,
            exc,
        )
        return False, f"NETCONF capability check failed: {exc}"


def discover_gpon_namespace(olt: OLTDevice) -> str | None:
    """Discover GPON YANG namespace from OLT capabilities.

    Args:
        olt: The OLT device to query.

    Returns:
        The GPON namespace string, or None if not found.
    """
    # Check cache first
    cached = _namespace_cache.get(str(olt.id))
    if cached is not None:
        return cached

    try:
        success, _message, capabilities = olt_netconf.test_connection(olt)
        if not success:
            return None

        namespace = _find_gpon_namespace_in_capabilities(capabilities)
        _namespace_cache[str(olt.id)] = namespace
        return namespace
    except Exception as exc:
        logger.warning(
            "Failed to discover GPON namespace on OLT %s: %s",
            olt.name,
            exc,
        )
        return None


def _find_gpon_namespace_in_capabilities(capabilities: list[str]) -> str | None:
    """Find GPON namespace in OLT NETCONF capabilities."""
    for cap in capabilities:
        for known_ns in HUAWEI_GPON_NAMESPACES:
            if known_ns in cap:
                return known_ns
        # Also check for partial matches in capability URNs
        if "gpon" in cap.lower():
            # Extract namespace from capability string
            # Format: urn:huawei:yang:huawei-gpon?module=huawei-gpon&revision=...
            match = re.match(r"([^?]+)", cap)
            if match:
                return match.group(1)
    return None


def authorize_ont(
    olt: OLTDevice,
    fsp: str,
    serial_number: str,
    *,
    line_profile_id: int,
    service_profile_id: int,
) -> tuple[bool, str, int | None]:
    """Authorize ONT via NETCONF (two-phase: edit-config + get ONT-ID).

    NETCONF edit-config returns only <ok/>, not the ONT-ID. We must query
    operational data afterward to find the assigned ONT-ID by serial number.

    Args:
        olt: The OLT device to authorize on.
        fsp: Frame/Slot/Port string, e.g. "0/2/1".
        serial_number: ONT serial in vendor format, e.g. "HWTC-7D4733C3".
        line_profile_id: OLT-local line profile ID.
        service_profile_id: OLT-local service profile ID.

    Returns:
        Tuple of (success, message, assigned_ont_id).
    """
    # Validate inputs
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err, None

    ok, err = _validate_serial(serial_number)
    if not ok:
        return False, err, None

    # Discover GPON namespace
    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT", None

    # Build authorization XML payload
    config_xml = _build_ont_add_xml(
        namespace=namespace,
        fsp=fsp,
        serial=serial_number,
        line_id=line_profile_id,
        srv_id=service_profile_id,
    )

    logger.info(
        "Authorizing ONT via NETCONF: olt=%s fsp=%s serial=%s line_profile=%d service_profile=%d",
        olt.name,
        fsp,
        serial_number,
        line_profile_id,
        service_profile_id,
    )

    # Push configuration via edit-config
    # Note: NETCONF edit-config returns only <ok/>, not the assigned ONT-ID.
    # The ONT-ID will be populated by the post-authorization SNMP sync task,
    # which already runs after authorization completes.
    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}", None
    except RPCError as exc:
        return _handle_rpc_error(exc, serial_number) + (None,)
    except Exception as exc:
        logger.error(
            "NETCONF authorization error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}", None

    logger.info(
        "ONT authorized via NETCONF: olt=%s fsp=%s serial=%s",
        olt.name,
        fsp,
        serial_number,
    )
    # Return None for ont_id - the post-auth sync will populate it via SNMP
    return (
        True,
        f"ONT {serial_number} authorized on port {fsp} via NETCONF",
        None,
    )


def deauthorize_ont(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
) -> tuple[bool, str]:
    """Deauthorize ONT via NETCONF edit-config.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID to delete.

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    config_xml = _build_ont_delete_xml(namespace, fsp, ont_id)

    logger.info(
        "Deauthorizing ONT via NETCONF: olt=%s fsp=%s ont_id=%d",
        olt.name,
        fsp,
        ont_id,
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}"
        logger.info(
            "ONT deauthorized via NETCONF: olt=%s fsp=%s ont_id=%d",
            olt.name,
            fsp,
            ont_id,
        )
        return True, f"ONT {ont_id} deleted from OLT via NETCONF"
    except RPCError as exc:
        return _handle_rpc_error(exc, str(ont_id))
    except Exception as exc:
        logger.error(
            "NETCONF deauthorization error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


def _build_ont_add_xml(
    namespace: str,
    fsp: str,
    serial: str,
    line_id: int,
    srv_id: int,
) -> str:
    """Build Huawei YANG XML payload for ONT authorization.

    The XML structure follows Huawei's GPON YANG model:
    - gpon/board[frame-id,slot-id]/port[port-id]/ont

    Args:
        namespace: The discovered GPON YANG namespace.
        fsp: Frame/Slot/Port string (e.g., "0/2/1").
        serial: ONT serial number (dashes will be removed).
        line_id: Line profile ID.
        srv_id: Service profile ID.

    Returns:
        XML configuration string.
    """
    parts = fsp.split("/")
    frame_id = parts[0]
    slot_id = parts[1]
    port_id = parts[2]

    # Remove dashes from serial for Huawei format
    clean_serial = serial.replace("-", "").upper()

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont>
          <serial-number>{clean_serial}</serial-number>
          <auth-type>sn-auth</auth-type>
          <ont-lineprofile-id>{line_id}</ont-lineprofile-id>
          <ont-srvprofile-id>{srv_id}</ont-srvprofile-id>
        </ont>
      </port>
    </board>
  </gpon>
</config>"""


def _build_ont_delete_xml(namespace: str, fsp: str, ont_id: int) -> str:
    """Build Huawei YANG XML payload for ONT deletion.

    Args:
        namespace: The discovered GPON YANG namespace.
        fsp: Frame/Slot/Port string.
        ont_id: ONT ID to delete.

    Returns:
        XML configuration string with delete operation.
    """
    parts = fsp.split("/")
    frame_id = parts[0]
    slot_id = parts[1]
    port_id = parts[2]

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete">
          <ont-id>{ont_id}</ont-id>
        </ont>
      </port>
    </board>
  </gpon>
</config>"""


def _handle_rpc_error(exc: RPCError, context: str) -> tuple[bool, str]:
    """Map NETCONF RPC errors to user-friendly messages.

    Args:
        exc: The RPC error from ncclient.
        context: Context string (serial number or ONT-ID) for the message.

    Returns:
        Tuple of (success=False, user_friendly_message).
    """
    error_tag = getattr(exc, "tag", "") or ""
    error_message = str(exc)

    # Map known error tags to user-friendly messages
    error_mappings = {
        "data-exists": f"ONT serial number {context} already registered on this OLT",
        "invalid-value": "Invalid parameter in authorization request",
        "access-denied": "NETCONF user lacks permission for ONT authorization",
        "lock-denied": "Another session holds the configuration lock",
        "resource-denied": "OLT resource limit reached for ONT authorization",
        "operation-failed": f"ONT authorization failed: {error_message}",
    }

    # Check for known error tags
    for tag, message in error_mappings.items():
        if tag in error_tag.lower():
            logger.warning(
                "NETCONF RPC error during ONT operation: tag=%s message=%s",
                error_tag,
                error_message,
            )
            return False, message

    # Check for common error patterns in message
    if (
        "already exists" in error_message.lower()
        or "sn already" in error_message.lower()
    ):
        return False, f"ONT serial number {context} already registered on this OLT"

    # Generic error
    logger.warning(
        "NETCONF RPC error: tag=%s message=%s",
        error_tag,
        error_message,
    )
    return False, f"NETCONF error: {error_message}"


# Validation helpers (same as SSH module for consistency)

_FSP_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{1,3}$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9\-]+$")
_FSP_PREFIX_RE = re.compile(r"^(?:x?g?pon|epon|port|gei|ge|eth)[-_]?", re.IGNORECASE)


def _normalize_fsp(fsp: str) -> str:
    """Normalize FSP by stripping common port name prefixes like 'pon-'."""
    if not fsp:
        return fsp
    return _FSP_PREFIX_RE.sub("", fsp.strip())


def _validate_fsp(fsp: str) -> tuple[bool, str]:
    """Validate Frame/Slot/Port format."""
    check_fsp = _normalize_fsp(fsp)
    if not _FSP_RE.match(check_fsp):
        return False, f"Invalid F/S/P format: {fsp!r} (expected digits/digits/digits)"
    return True, ""


def _validate_serial(serial_number: str) -> tuple[bool, str]:
    """Validate ONT serial number format."""
    if not serial_number or not _SERIAL_RE.match(serial_number):
        return False, f"Invalid serial number format: {serial_number!r}"
    return True, ""


def clear_namespace_cache(olt_id: str | None = None) -> None:
    """Clear the GPON namespace cache.

    Args:
        olt_id: Specific OLT ID to clear, or None to clear all.
    """
    if olt_id is None:
        _namespace_cache.clear()
    elif olt_id in _namespace_cache:
        del _namespace_cache[olt_id]


def configure_ont_iphost(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    vlan_id: int,
    ip_mode: str = "dhcp",
    priority: int | None = None,
    ip_address: str | None = None,
    subnet: str | None = None,
    gateway: str | None = None,
) -> tuple[bool, str]:
    """Configure ONT management IP (IPHOST) via NETCONF.

    NETCONF is preferred over SSH CLI because it avoids terminal escape
    sequence corruption issues that occur with long commands on some
    Huawei OLT terminals.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string, e.g. "0/1/0".
        ont_id: The ONT ID on that port.
        vlan_id: Management VLAN ID.
        ip_mode: "dhcp" or "static".
        priority: Optional 802.1p priority (0-7).
        ip_address: IP address (required for static mode).
        subnet: Subnet mask (required for static mode).
        gateway: Gateway IP (required for static mode).

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    if ip_mode != "dhcp":
        if not ip_address or not subnet or not gateway:
            return False, "Static IP mode requires ip_address, subnet, and gateway"

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    config_xml = _build_iphost_xml(
        namespace=namespace,
        fsp=fsp,
        ont_id=ont_id,
        vlan_id=vlan_id,
        ip_mode=ip_mode,
        priority=priority,
        ip_address=ip_address,
        subnet=subnet,
        gateway=gateway,
    )

    logger.info(
        "Configuring IPHOST via NETCONF: olt=%s fsp=%s ont_id=%d vlan=%d mode=%s ip=%s",
        olt.name,
        fsp,
        ont_id,
        vlan_id,
        ip_mode,
        ip_address or "dhcp",
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}"
    except RPCError as exc:
        error_message = str(exc)
        if (
            "already exists" in error_message.lower()
            or "repeatedly" in error_message.lower()
        ):
            logger.info(
                "IPHOST config already exists for ONT %d on OLT %s (VLAN %d)",
                ont_id,
                olt.name,
                vlan_id,
            )
            return (
                True,
                f"Management IP already configured ({ip_mode} on VLAN {vlan_id})",
            )
        logger.warning(
            "NETCONF RPC error during IPHOST config: %s",
            error_message,
        )
        return False, f"NETCONF error: {error_message}"
    except Exception as exc:
        logger.error(
            "NETCONF IPHOST error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"

    logger.info(
        "IPHOST configured via NETCONF: olt=%s fsp=%s ont_id=%d vlan=%d",
        olt.name,
        fsp,
        ont_id,
        vlan_id,
    )
    return True, f"Management IP configured via NETCONF ({ip_mode} on VLAN {vlan_id})"


def _build_iphost_xml(
    namespace: str,
    fsp: str,
    ont_id: int,
    vlan_id: int,
    ip_mode: str,
    priority: int | None,
    ip_address: str | None,
    subnet: str | None,
    gateway: str | None,
) -> str:
    """Build Huawei YANG XML payload for ONT IPHOST configuration.

    The XML structure follows Huawei's GPON YANG model for iphost config.
    """
    parts = fsp.split("/")
    frame_id = parts[0]
    slot_id = parts[1]
    port_id = parts[2]

    priority_element = (
        f"<priority>{priority}</priority>" if priority is not None else ""
    )

    if ip_mode == "dhcp":
        ip_config = "<config-type>dhcp</config-type>"
    else:
        ip_config = f"""<config-type>static</config-type>
          <ip-address>{ip_address}</ip-address>
          <subnet-mask>{subnet}</subnet-mask>
          <gateway>{gateway}</gateway>"""

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont>
          <ont-id>{ont_id}</ont-id>
          <iphost>
            <ip-index>0</ip-index>
            {ip_config}
            <vlan>{vlan_id}</vlan>
            {priority_element}
          </iphost>
        </ont>
      </port>
    </board>
  </gpon>
</config>"""
