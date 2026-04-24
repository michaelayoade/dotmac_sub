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


# ============================================================================
# ONT Query Operations
# ============================================================================


def find_ont_by_serial(
    olt: OLTDevice,
    serial_number: str,
) -> tuple[bool, str, dict | None]:
    """Find ONT on OLT by serial number using NETCONF get.

    Args:
        olt: The OLT device.
        serial_number: ONT serial number to search for.

    Returns:
        Tuple of (success, message, ont_info_dict or None).
    """
    ok, err = _validate_serial(serial_number)
    if not ok:
        return False, err, None

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT", None

    clean_serial = serial_number.replace("-", "").upper()

    # Build filter for querying ONT by serial
    filter_xml = f"""<gpon xmlns="{namespace}">
  <board>
    <port>
      <ont>
        <serial-number>{clean_serial}</serial-number>
      </ont>
    </port>
  </board>
</gpon>"""

    try:
        success, message, data = olt_netconf.get_config_filtered(olt, filter_xml)
        if not success:
            return False, f"NETCONF get-config failed: {message}", None

        # Parse the response to extract ONT info
        ont_info = _parse_ont_info_from_xml(data, clean_serial)
        if ont_info:
            return True, f"Found ONT with serial {serial_number}", ont_info
        return False, f"ONT with serial {serial_number} not found", None

    except RPCError as exc:
        return False, f"NETCONF error: {exc}", None
    except Exception as exc:
        logger.error(
            "NETCONF find_ont_by_serial error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}", None


def get_service_ports(
    olt: OLTDevice,
    fsp: str,
) -> tuple[bool, str, list[dict]]:
    """Get service ports for a PON port via NETCONF.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.

    Returns:
        Tuple of (success, message, list_of_service_port_dicts).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err, []

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT", []

    parts = fsp.split("/")
    frame_id = parts[0]
    slot_id = parts[1]
    port_id = parts[2]

    filter_xml = f"""<gpon xmlns="{namespace}">
  <board>
    <frame-id>{frame_id}</frame-id>
    <slot-id>{slot_id}</slot-id>
    <port>
      <port-id>{port_id}</port-id>
      <service-port/>
    </port>
  </board>
</gpon>"""

    try:
        success, message, data = olt_netconf.get_config_filtered(olt, filter_xml)
        if not success:
            return False, f"NETCONF get-config failed: {message}", []

        service_ports = _parse_service_ports_from_xml(data)
        return True, f"Found {len(service_ports)} service ports", service_ports

    except RPCError as exc:
        return False, f"NETCONF error: {exc}", []
    except Exception as exc:
        logger.error(
            "NETCONF get_service_ports error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}", []


def get_autofind_onts(
    olt: OLTDevice,
) -> tuple[bool, str, list[dict]]:
    """Get autofind (unauthorized) ONTs via NETCONF.

    Args:
        olt: The OLT device.

    Returns:
        Tuple of (success, message, list_of_autofind_ont_dicts).
    """
    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT", []

    # Autofind ONTs are typically in a separate container
    filter_xml = f"""<gpon xmlns="{namespace}">
  <autofind-ont/>
</gpon>"""

    try:
        success, message, data = olt_netconf.get_filtered(olt, filter_xml)
        if not success:
            return False, f"NETCONF get failed: {message}", []

        autofind_onts = _parse_autofind_onts_from_xml(data)
        return True, f"Found {len(autofind_onts)} autofind ONTs", autofind_onts

    except RPCError as exc:
        return False, f"NETCONF error: {exc}", []
    except Exception as exc:
        logger.error(
            "NETCONF get_autofind_onts error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}", []


def get_line_profiles(
    olt: OLTDevice,
) -> tuple[bool, str, list[dict]]:
    """Get line profiles via NETCONF.

    Args:
        olt: The OLT device.

    Returns:
        Tuple of (success, message, list_of_profile_dicts).
    """
    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT", []

    filter_xml = f"""<gpon xmlns="{namespace}">
  <ont-lineprofile/>
</gpon>"""

    try:
        success, message, data = olt_netconf.get_config_filtered(olt, filter_xml)
        if not success:
            return False, f"NETCONF get-config failed: {message}", []

        profiles = _parse_profiles_from_xml(data, "ont-lineprofile")
        return True, f"Found {len(profiles)} line profiles", profiles

    except RPCError as exc:
        return False, f"NETCONF error: {exc}", []
    except Exception as exc:
        logger.error(
            "NETCONF get_line_profiles error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}", []


def get_service_profiles(
    olt: OLTDevice,
) -> tuple[bool, str, list[dict]]:
    """Get service profiles via NETCONF.

    Args:
        olt: The OLT device.

    Returns:
        Tuple of (success, message, list_of_profile_dicts).
    """
    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT", []

    filter_xml = f"""<gpon xmlns="{namespace}">
  <ont-srvprofile/>
</gpon>"""

    try:
        success, message, data = olt_netconf.get_config_filtered(olt, filter_xml)
        if not success:
            return False, f"NETCONF get-config failed: {message}", []

        profiles = _parse_profiles_from_xml(data, "ont-srvprofile")
        return True, f"Found {len(profiles)} service profiles", profiles

    except RPCError as exc:
        return False, f"NETCONF error: {exc}", []
    except Exception as exc:
        logger.error(
            "NETCONF get_service_profiles error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}", []


def get_tr069_profiles(
    olt: OLTDevice,
) -> tuple[bool, str, list[dict]]:
    """Get TR-069 management profiles via NETCONF.

    Args:
        olt: The OLT device.

    Returns:
        Tuple of (success, message, list_of_profile_dicts).
    """
    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT", []

    filter_xml = f"""<gpon xmlns="{namespace}">
  <tr069-profile/>
</gpon>"""

    try:
        success, message, data = olt_netconf.get_config_filtered(olt, filter_xml)
        if not success:
            return False, f"NETCONF get-config failed: {message}", []

        profiles = _parse_profiles_from_xml(data, "tr069-profile")
        return True, f"Found {len(profiles)} TR-069 profiles", profiles

    except RPCError as exc:
        return False, f"NETCONF error: {exc}", []
    except Exception as exc:
        logger.error(
            "NETCONF get_tr069_profiles error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}", []


# ============================================================================
# ONT Configuration Operations
# ============================================================================


def bind_tr069_profile(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    profile_id: int,
) -> tuple[bool, str]:
    """Bind TR-069 management profile to ONT via NETCONF.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID.
        profile_id: TR-069 profile ID to bind.

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    config_xml = _build_tr069_bind_xml(namespace, fsp, ont_id, profile_id)

    logger.info(
        "Binding TR-069 profile via NETCONF: olt=%s fsp=%s ont_id=%d profile_id=%d",
        olt.name,
        fsp,
        ont_id,
        profile_id,
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}"

        return True, f"TR-069 profile {profile_id} bound to ONT {ont_id}"

    except RPCError as exc:
        return _handle_rpc_error(exc, str(ont_id))
    except Exception as exc:
        logger.error(
            "NETCONF bind_tr069_profile error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


def unbind_tr069_profile(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
) -> tuple[bool, str]:
    """Unbind TR-069 management profile from ONT via NETCONF.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID.

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    config_xml = _build_tr069_unbind_xml(namespace, fsp, ont_id)

    logger.info(
        "Unbinding TR-069 profile via NETCONF: olt=%s fsp=%s ont_id=%d",
        olt.name,
        fsp,
        ont_id,
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}"

        return True, f"TR-069 profile unbound from ONT {ont_id}"

    except RPCError as exc:
        return _handle_rpc_error(exc, str(ont_id))
    except Exception as exc:
        logger.error(
            "NETCONF unbind_tr069_profile error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


def create_service_port(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    gem_index: int,
    vlan_id: int,
    user_vlan: int | str | None = None,
    tag_transform: str = "translate",
    port_index: int | None = None,
) -> tuple[bool, str, int | None]:
    """Create service port for ONT via NETCONF.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID.
        gem_index: GEM port index.
        vlan_id: Service VLAN ID.
        user_vlan: User-side VLAN (optional).
        tag_transform: Tag transformation mode (translate, transparent, etc.).
        port_index: Specific port index (auto-assigned if None).

    Returns:
        Tuple of (success, message, assigned_port_index or None).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err, None

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT", None

    config_xml = _build_service_port_create_xml(
        namespace=namespace,
        fsp=fsp,
        ont_id=ont_id,
        gem_index=gem_index,
        vlan_id=vlan_id,
        user_vlan=user_vlan,
        tag_transform=tag_transform,
        port_index=port_index,
    )

    logger.info(
        "Creating service port via NETCONF: olt=%s fsp=%s ont_id=%d gem=%d vlan=%d",
        olt.name,
        fsp,
        ont_id,
        gem_index,
        vlan_id,
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}", None

        # Port index is returned only if explicitly set; otherwise OLT assigns
        return True, f"Service port created (VLAN {vlan_id})", port_index

    except RPCError as exc:
        error_msg = str(exc)
        if "already exists" in error_msg.lower():
            return True, f"Service port already exists (VLAN {vlan_id})", port_index
        return _handle_rpc_error(exc, str(ont_id)) + (None,)
    except Exception as exc:
        logger.error(
            "NETCONF create_service_port error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}", None


def delete_service_port(
    olt: OLTDevice,
    port_index: int,
) -> tuple[bool, str]:
    """Delete service port via NETCONF.

    Args:
        olt: The OLT device.
        port_index: Service port index to delete.

    Returns:
        Tuple of (success, message).
    """
    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    config_xml = _build_service_port_delete_xml(namespace, port_index)

    logger.info(
        "Deleting service port via NETCONF: olt=%s port_index=%d",
        olt.name,
        port_index,
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}"

        return True, f"Service port {port_index} deleted"

    except RPCError as exc:
        return _handle_rpc_error(exc, str(port_index))
    except Exception as exc:
        logger.error(
            "NETCONF delete_service_port error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


def update_ont_profiles(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    line_profile_id: int | None = None,
    service_profile_id: int | None = None,
) -> tuple[bool, str]:
    """Update ONT line and/or service profile via NETCONF.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID.
        line_profile_id: New line profile ID (optional).
        service_profile_id: New service profile ID (optional).

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    if line_profile_id is None and service_profile_id is None:
        return False, "At least one of line_profile_id or service_profile_id required"

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    config_xml = _build_ont_profile_update_xml(
        namespace=namespace,
        fsp=fsp,
        ont_id=ont_id,
        line_profile_id=line_profile_id,
        service_profile_id=service_profile_id,
    )

    logger.info(
        "Updating ONT profiles via NETCONF: olt=%s fsp=%s ont_id=%d line=%s srv=%s",
        olt.name,
        fsp,
        ont_id,
        line_profile_id,
        service_profile_id,
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}"

        return True, f"ONT {ont_id} profiles updated"

    except RPCError as exc:
        return _handle_rpc_error(exc, str(ont_id))
    except Exception as exc:
        logger.error(
            "NETCONF update_ont_profiles error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


# ============================================================================
# Extended ONT Configuration
# ============================================================================


def configure_internet_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
) -> tuple[bool, str]:
    """Configure ONT internet-config via NETCONF.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID.
        ip_index: IP index (default 0).

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    config_xml = _build_internet_config_xml(namespace, fsp, ont_id, ip_index)

    logger.info(
        "Configuring internet-config via NETCONF: olt=%s fsp=%s ont_id=%d ip_index=%d",
        olt.name,
        fsp,
        ont_id,
        ip_index,
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}"

        return True, f"Internet config enabled on ONT {ont_id}"

    except RPCError as exc:
        error_msg = str(exc)
        if "already" in error_msg.lower():
            return True, f"Internet config already enabled on ONT {ont_id}"
        return _handle_rpc_error(exc, str(ont_id))
    except Exception as exc:
        logger.error(
            "NETCONF configure_internet_config error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


def configure_wan_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
    profile_id: int = 0,
) -> tuple[bool, str]:
    """Configure ONT WAN config via NETCONF.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID.
        ip_index: IP index (default 0).
        profile_id: WAN profile ID (default 0).

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    config_xml = _build_wan_config_xml(namespace, fsp, ont_id, ip_index, profile_id)

    logger.info(
        "Configuring wan-config via NETCONF: olt=%s fsp=%s ont_id=%d profile=%d",
        olt.name,
        fsp,
        ont_id,
        profile_id,
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}"

        return True, f"WAN config enabled on ONT {ont_id}"

    except RPCError as exc:
        error_msg = str(exc)
        if "already" in error_msg.lower():
            return True, f"WAN config already enabled on ONT {ont_id}"
        return _handle_rpc_error(exc, str(ont_id))
    except Exception as exc:
        logger.error(
            "NETCONF configure_wan_config error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


def configure_pppoe(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int,
    vlan_id: int,
    username: str,
    password: str,
) -> tuple[bool, str]:
    """Configure PPPoE on ONT via NETCONF.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID.
        ip_index: IP index.
        vlan_id: PPPoE VLAN ID.
        username: PPPoE username.
        password: PPPoE password.

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    config_xml = _build_pppoe_config_xml(
        namespace, fsp, ont_id, ip_index, vlan_id, username, password
    )

    logger.info(
        "Configuring PPPoE via NETCONF: olt=%s fsp=%s ont_id=%d vlan=%d user=%s",
        olt.name,
        fsp,
        ont_id,
        vlan_id,
        username,
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}"

        return True, f"PPPoE configured on ONT {ont_id} (VLAN {vlan_id})"

    except RPCError as exc:
        return _handle_rpc_error(exc, str(ont_id))
    except Exception as exc:
        logger.error(
            "NETCONF configure_pppoe error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


def configure_port_native_vlan(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    eth_port: int,
    vlan_id: int,
    priority: int = 0,
) -> tuple[bool, str]:
    """Configure native VLAN on ONT Ethernet port via NETCONF.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID.
        eth_port: Ethernet port number.
        vlan_id: Native VLAN ID.
        priority: 802.1p priority (default 0).

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    config_xml = _build_port_native_vlan_xml(
        namespace, fsp, ont_id, eth_port, vlan_id, priority
    )

    logger.info(
        "Configuring port native VLAN via NETCONF: olt=%s fsp=%s ont_id=%d eth=%d vlan=%d",
        olt.name,
        fsp,
        ont_id,
        eth_port,
        vlan_id,
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}"

        return True, f"Native VLAN {vlan_id} set on port {eth_port}"

    except RPCError as exc:
        return _handle_rpc_error(exc, str(ont_id))
    except Exception as exc:
        logger.error(
            "NETCONF configure_port_native_vlan error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


# ============================================================================
# Clear Operations
# ============================================================================


def clear_iphost_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
) -> tuple[bool, str]:
    """Clear IPHOST configuration from ONT via NETCONF.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID.
        ip_index: IP index to clear (default 0).

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    config_xml = _build_iphost_delete_xml(namespace, fsp, ont_id, ip_index)

    logger.info(
        "Clearing IPHOST config via NETCONF: olt=%s fsp=%s ont_id=%d ip_index=%d",
        olt.name,
        fsp,
        ont_id,
        ip_index,
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}"

        return True, f"IPHOST config cleared from ONT {ont_id}"

    except RPCError as exc:
        error_msg = str(exc)
        if "does not exist" in error_msg.lower() or "not found" in error_msg.lower():
            return True, f"IPHOST config already cleared from ONT {ont_id}"
        return _handle_rpc_error(exc, str(ont_id))
    except Exception as exc:
        logger.error(
            "NETCONF clear_iphost_config error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


def clear_internet_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
) -> tuple[bool, str]:
    """Clear internet-config from ONT via NETCONF.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID.
        ip_index: IP index to clear (default 0).

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    config_xml = _build_internet_config_delete_xml(namespace, fsp, ont_id, ip_index)

    logger.info(
        "Clearing internet-config via NETCONF: olt=%s fsp=%s ont_id=%d",
        olt.name,
        fsp,
        ont_id,
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}"

        return True, f"Internet config cleared from ONT {ont_id}"

    except RPCError as exc:
        error_msg = str(exc)
        if "does not exist" in error_msg.lower() or "not found" in error_msg.lower():
            return True, f"Internet config already cleared from ONT {ont_id}"
        return _handle_rpc_error(exc, str(ont_id))
    except Exception as exc:
        logger.error(
            "NETCONF clear_internet_config error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


def clear_wan_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
) -> tuple[bool, str]:
    """Clear WAN config from ONT via NETCONF.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID.
        ip_index: IP index to clear (default 0).

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    config_xml = _build_wan_config_delete_xml(namespace, fsp, ont_id, ip_index)

    logger.info(
        "Clearing wan-config via NETCONF: olt=%s fsp=%s ont_id=%d",
        olt.name,
        fsp,
        ont_id,
    )

    try:
        success, message = olt_netconf.edit_config(olt, config_xml)
        if not success:
            return False, f"NETCONF edit-config failed: {message}"

        return True, f"WAN config cleared from ONT {ont_id}"

    except RPCError as exc:
        error_msg = str(exc)
        if "does not exist" in error_msg.lower() or "not found" in error_msg.lower():
            return True, f"WAN config already cleared from ONT {ont_id}"
        return _handle_rpc_error(exc, str(ont_id))
    except Exception as exc:
        logger.error(
            "NETCONF clear_wan_config error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


# ============================================================================
# ONT Lifecycle Operations
# ============================================================================


def reboot_ont(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
) -> tuple[bool, str]:
    """Reboot ONT via NETCONF RPC.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID.

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    rpc_xml = _build_ont_reboot_rpc(namespace, fsp, ont_id)

    logger.info(
        "Rebooting ONT via NETCONF: olt=%s fsp=%s ont_id=%d",
        olt.name,
        fsp,
        ont_id,
    )

    try:
        success, message = olt_netconf.dispatch_rpc(olt, rpc_xml)
        if not success:
            return False, f"NETCONF RPC failed: {message}"

        return True, f"ONT {ont_id} reboot initiated"

    except RPCError as exc:
        return _handle_rpc_error(exc, str(ont_id))
    except Exception as exc:
        logger.error(
            "NETCONF reboot_ont error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


def factory_reset_ont(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
) -> tuple[bool, str]:
    """Factory reset ONT via NETCONF RPC.

    Args:
        olt: The OLT device.
        fsp: Frame/Slot/Port string.
        ont_id: The ONT ID.

    Returns:
        Tuple of (success, message).
    """
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err

    namespace = discover_gpon_namespace(olt)
    if namespace is None:
        return False, "Could not discover GPON YANG namespace on OLT"

    rpc_xml = _build_ont_factory_reset_rpc(namespace, fsp, ont_id)

    logger.info(
        "Factory resetting ONT via NETCONF: olt=%s fsp=%s ont_id=%d",
        olt.name,
        fsp,
        ont_id,
    )

    try:
        success, message = olt_netconf.dispatch_rpc(olt, rpc_xml)
        if not success:
            return False, f"NETCONF RPC failed: {message}"

        return True, f"ONT {ont_id} factory reset initiated"

    except RPCError as exc:
        return _handle_rpc_error(exc, str(ont_id))
    except Exception as exc:
        logger.error(
            "NETCONF factory_reset_ont error on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"NETCONF error: {type(exc).__name__}: {exc}"


# ============================================================================
# XML Builder Helpers
# ============================================================================


def _build_tr069_bind_xml(
    namespace: str, fsp: str, ont_id: int, profile_id: int
) -> str:
    """Build XML for binding TR-069 profile to ONT."""
    parts = fsp.split("/")
    frame_id, slot_id, port_id = parts[0], parts[1], parts[2]

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont>
          <ont-id>{ont_id}</ont-id>
          <tr069-profile-id>{profile_id}</tr069-profile-id>
        </ont>
      </port>
    </board>
  </gpon>
</config>"""


def _build_tr069_unbind_xml(namespace: str, fsp: str, ont_id: int) -> str:
    """Build XML for unbinding TR-069 profile from ONT."""
    parts = fsp.split("/")
    frame_id, slot_id, port_id = parts[0], parts[1], parts[2]

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont>
          <ont-id>{ont_id}</ont-id>
          <tr069-profile-id xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete"/>
        </ont>
      </port>
    </board>
  </gpon>
</config>"""


def _build_service_port_create_xml(
    namespace: str,
    fsp: str,
    ont_id: int,
    gem_index: int,
    vlan_id: int,
    user_vlan: int | str | None,
    tag_transform: str,
    port_index: int | None,
) -> str:
    """Build XML for creating service port."""
    parts = fsp.split("/")
    frame_id, slot_id, port_id = parts[0], parts[1], parts[2]

    port_index_elem = f"<port-index>{port_index}</port-index>" if port_index else ""
    user_vlan_elem = f"<user-vlan>{user_vlan}</user-vlan>" if user_vlan else ""

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont>
          <ont-id>{ont_id}</ont-id>
          <service-port>
            {port_index_elem}
            <gem-index>{gem_index}</gem-index>
            <vlan-id>{vlan_id}</vlan-id>
            {user_vlan_elem}
            <tag-transform>{tag_transform}</tag-transform>
          </service-port>
        </ont>
      </port>
    </board>
  </gpon>
</config>"""


def _build_service_port_delete_xml(namespace: str, port_index: int) -> str:
    """Build XML for deleting service port."""
    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <service-port xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete">
      <port-index>{port_index}</port-index>
    </service-port>
  </gpon>
</config>"""


def _build_ont_profile_update_xml(
    namespace: str,
    fsp: str,
    ont_id: int,
    line_profile_id: int | None,
    service_profile_id: int | None,
) -> str:
    """Build XML for updating ONT profiles."""
    parts = fsp.split("/")
    frame_id, slot_id, port_id = parts[0], parts[1], parts[2]

    line_elem = (
        f"<ont-lineprofile-id>{line_profile_id}</ont-lineprofile-id>"
        if line_profile_id is not None
        else ""
    )
    srv_elem = (
        f"<ont-srvprofile-id>{service_profile_id}</ont-srvprofile-id>"
        if service_profile_id is not None
        else ""
    )

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont>
          <ont-id>{ont_id}</ont-id>
          {line_elem}
          {srv_elem}
        </ont>
      </port>
    </board>
  </gpon>
</config>"""


def _build_internet_config_xml(
    namespace: str, fsp: str, ont_id: int, ip_index: int
) -> str:
    """Build XML for internet-config enable."""
    parts = fsp.split("/")
    frame_id, slot_id, port_id = parts[0], parts[1], parts[2]

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont>
          <ont-id>{ont_id}</ont-id>
          <internet-config>
            <ip-index>{ip_index}</ip-index>
            <enable>true</enable>
          </internet-config>
        </ont>
      </port>
    </board>
  </gpon>
</config>"""


def _build_wan_config_xml(
    namespace: str, fsp: str, ont_id: int, ip_index: int, profile_id: int
) -> str:
    """Build XML for WAN config."""
    parts = fsp.split("/")
    frame_id, slot_id, port_id = parts[0], parts[1], parts[2]

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont>
          <ont-id>{ont_id}</ont-id>
          <wan-config>
            <ip-index>{ip_index}</ip-index>
            <profile-id>{profile_id}</profile-id>
          </wan-config>
        </ont>
      </port>
    </board>
  </gpon>
</config>"""


def _build_pppoe_config_xml(
    namespace: str,
    fsp: str,
    ont_id: int,
    ip_index: int,
    vlan_id: int,
    username: str,
    password: str,
) -> str:
    """Build XML for PPPoE configuration."""
    parts = fsp.split("/")
    frame_id, slot_id, port_id = parts[0], parts[1], parts[2]

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont>
          <ont-id>{ont_id}</ont-id>
          <pppoe>
            <ip-index>{ip_index}</ip-index>
            <vlan-id>{vlan_id}</vlan-id>
            <username>{username}</username>
            <password>{password}</password>
          </pppoe>
        </ont>
      </port>
    </board>
  </gpon>
</config>"""


def _build_port_native_vlan_xml(
    namespace: str,
    fsp: str,
    ont_id: int,
    eth_port: int,
    vlan_id: int,
    priority: int,
) -> str:
    """Build XML for port native VLAN configuration."""
    parts = fsp.split("/")
    frame_id, slot_id, port_id = parts[0], parts[1], parts[2]

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont>
          <ont-id>{ont_id}</ont-id>
          <eth-port>
            <port-id>{eth_port}</port-id>
            <native-vlan>{vlan_id}</native-vlan>
            <priority>{priority}</priority>
          </eth-port>
        </ont>
      </port>
    </board>
  </gpon>
</config>"""


def _build_iphost_delete_xml(
    namespace: str, fsp: str, ont_id: int, ip_index: int
) -> str:
    """Build XML for deleting IPHOST config."""
    parts = fsp.split("/")
    frame_id, slot_id, port_id = parts[0], parts[1], parts[2]

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont>
          <ont-id>{ont_id}</ont-id>
          <iphost xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete">
            <ip-index>{ip_index}</ip-index>
          </iphost>
        </ont>
      </port>
    </board>
  </gpon>
</config>"""


def _build_internet_config_delete_xml(
    namespace: str, fsp: str, ont_id: int, ip_index: int
) -> str:
    """Build XML for deleting internet-config."""
    parts = fsp.split("/")
    frame_id, slot_id, port_id = parts[0], parts[1], parts[2]

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont>
          <ont-id>{ont_id}</ont-id>
          <internet-config xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete">
            <ip-index>{ip_index}</ip-index>
          </internet-config>
        </ont>
      </port>
    </board>
  </gpon>
</config>"""


def _build_wan_config_delete_xml(
    namespace: str, fsp: str, ont_id: int, ip_index: int
) -> str:
    """Build XML for deleting WAN config."""
    parts = fsp.split("/")
    frame_id, slot_id, port_id = parts[0], parts[1], parts[2]

    return f"""<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <gpon xmlns="{namespace}">
    <board>
      <frame-id>{frame_id}</frame-id>
      <slot-id>{slot_id}</slot-id>
      <port>
        <port-id>{port_id}</port-id>
        <ont>
          <ont-id>{ont_id}</ont-id>
          <wan-config xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete">
            <ip-index>{ip_index}</ip-index>
          </wan-config>
        </ont>
      </port>
    </board>
  </gpon>
</config>"""


def _build_ont_reboot_rpc(namespace: str, fsp: str, ont_id: int) -> str:
    """Build RPC XML for ONT reboot."""
    parts = fsp.split("/")
    frame_id, slot_id, port_id = parts[0], parts[1], parts[2]

    return f"""<ont-reboot xmlns="{namespace}">
  <frame-id>{frame_id}</frame-id>
  <slot-id>{slot_id}</slot-id>
  <port-id>{port_id}</port-id>
  <ont-id>{ont_id}</ont-id>
</ont-reboot>"""


def _build_ont_factory_reset_rpc(namespace: str, fsp: str, ont_id: int) -> str:
    """Build RPC XML for ONT factory reset."""
    parts = fsp.split("/")
    frame_id, slot_id, port_id = parts[0], parts[1], parts[2]

    return f"""<ont-reset xmlns="{namespace}">
  <frame-id>{frame_id}</frame-id>
  <slot-id>{slot_id}</slot-id>
  <port-id>{port_id}</port-id>
  <ont-id>{ont_id}</ont-id>
  <factory-reset>true</factory-reset>
</ont-reset>"""


# ============================================================================
# XML Parsing Helpers
# ============================================================================


def _parse_ont_info_from_xml(xml_data: str, serial: str) -> dict | None:
    """Parse ONT info from NETCONF get response XML."""
    import defusedxml.ElementTree as ET

    try:
        root = ET.fromstring(xml_data)
        # Search for ont element with matching serial
        for ont in root.iter():
            if ont.tag.endswith("ont"):
                ont_serial = None
                ont_id = None
                fsp = None
                for child in ont:
                    tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if tag == "serial-number":
                        ont_serial = child.text
                    elif tag == "ont-id":
                        ont_id = int(child.text) if child.text else None

                if ont_serial and ont_serial.upper() == serial.upper():
                    return {
                        "serial_number": ont_serial,
                        "ont_id": ont_id,
                        "fsp": fsp,
                    }
    except Exception as exc:
        logger.warning("Failed to parse ONT info from XML: %s", exc)

    return None


def _parse_service_ports_from_xml(xml_data: str) -> list[dict]:
    """Parse service ports from NETCONF response XML."""
    import defusedxml.ElementTree as ET

    ports = []
    try:
        root = ET.fromstring(xml_data)
        for sp in root.iter():
            if sp.tag.endswith("service-port"):
                port_info = {}
                for child in sp:
                    tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    port_info[tag] = child.text
                if port_info:
                    ports.append(port_info)
    except Exception as exc:
        logger.warning("Failed to parse service ports from XML: %s", exc)

    return ports


def _parse_autofind_onts_from_xml(xml_data: str) -> list[dict]:
    """Parse autofind ONTs from NETCONF response XML."""
    import defusedxml.ElementTree as ET

    onts = []
    try:
        root = ET.fromstring(xml_data)
        for ont in root.iter():
            if ont.tag.endswith("autofind-ont") or ont.tag.endswith("ont"):
                ont_info = {}
                for child in ont:
                    tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    ont_info[tag] = child.text
                if ont_info and "serial-number" in ont_info:
                    onts.append(ont_info)
    except Exception as exc:
        logger.warning("Failed to parse autofind ONTs from XML: %s", exc)

    return onts


def _parse_profiles_from_xml(xml_data: str, profile_type: str) -> list[dict]:
    """Parse profiles from NETCONF response XML."""
    import defusedxml.ElementTree as ET

    profiles = []
    try:
        root = ET.fromstring(xml_data)
        for profile in root.iter():
            if profile.tag.endswith(profile_type):
                profile_info = {}
                for child in profile:
                    tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    profile_info[tag] = child.text
                if profile_info:
                    profiles.append(profile_info)
    except Exception as exc:
        logger.warning("Failed to parse %s from XML: %s", profile_type, exc)

    return profiles
