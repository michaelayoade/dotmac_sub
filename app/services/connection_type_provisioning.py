"""Connection-type-specific provisioning logic.

Generates different RADIUS attributes and NAS commands based on the
subscription's connection type (PPPoE, IPoE/DHCP Option 82, DHCP,
Static, Hotspot).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.catalog import (
    ConnectionType,
    NasConnectionRule,
    NasDevice,
    RadiusProfile,
    Subscription,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection type → default RADIUS attributes
# ---------------------------------------------------------------------------

# Standard RADIUS attribute constants
_SERVICE_TYPE_FRAMED = "Framed-User"
_SERVICE_TYPE_LOGIN = "Login-User"
_FRAMED_PROTOCOL_PPP = "PPP"
_NAS_PORT_TYPE_VIRTUAL = "Virtual"
_NAS_PORT_TYPE_ETHERNET = "Ethernet"


def _base_pppoe_attributes(
    profile: RadiusProfile | None,
    subscription: Subscription,
) -> list[dict[str, str]]:
    """Generate RADIUS reply attributes for PPPoE connections."""
    attrs: list[dict[str, str]] = [
        {"attribute": "Service-Type", "op": ":=", "value": _SERVICE_TYPE_FRAMED},
        {"attribute": "Framed-Protocol", "op": ":=", "value": _FRAMED_PROTOCOL_PPP},
    ]
    if profile:
        if profile.ip_pool_name:
            attrs.append({"attribute": "Framed-Pool", "op": ":=", "value": profile.ip_pool_name})
        if profile.ipv6_pool_name:
            attrs.append({"attribute": "Delegated-IPv6-Prefix-Pool", "op": ":=", "value": profile.ipv6_pool_name})
        if profile.session_timeout:
            attrs.append({"attribute": "Session-Timeout", "op": ":=", "value": str(profile.session_timeout)})
        if profile.idle_timeout:
            attrs.append({"attribute": "Idle-Timeout", "op": ":=", "value": str(profile.idle_timeout)})
        if profile.simultaneous_use:
            attrs.append({"attribute": "Simultaneous-Use", "op": ":=", "value": str(profile.simultaneous_use)})
    if subscription.ipv4_address:
        attrs.append({"attribute": "Framed-IP-Address", "op": ":=", "value": subscription.ipv4_address})
    return attrs


def _base_dhcp_attributes(
    profile: RadiusProfile | None,
    subscription: Subscription,
) -> list[dict[str, str]]:
    """Generate RADIUS reply attributes for plain DHCP connections."""
    attrs: list[dict[str, str]] = [
        {"attribute": "Service-Type", "op": ":=", "value": _SERVICE_TYPE_FRAMED},
    ]
    if profile:
        if profile.ip_pool_name:
            attrs.append({"attribute": "Framed-Pool", "op": ":=", "value": profile.ip_pool_name})
        if profile.session_timeout:
            attrs.append({"attribute": "Session-Timeout", "op": ":=", "value": str(profile.session_timeout)})
    if subscription.ipv4_address:
        attrs.append({"attribute": "Framed-IP-Address", "op": ":=", "value": subscription.ipv4_address})
    if subscription.mac_address:
        attrs.append({"attribute": "Calling-Station-Id", "op": ":=", "value": subscription.mac_address})
    return attrs


def _base_ipoe_attributes(
    profile: RadiusProfile | None,
    subscription: Subscription,
) -> list[dict[str, str]]:
    """Generate RADIUS reply attributes for IPoE/DHCP Option 82 connections.

    IPoE uses MAC-based or Option 82-based authentication instead of
    PPPoE login/password. The relay agent info (circuit-id, remote-id)
    is matched to identify the subscriber.
    """
    attrs: list[dict[str, str]] = [
        {"attribute": "Service-Type", "op": ":=", "value": _SERVICE_TYPE_FRAMED},
        {"attribute": "NAS-Port-Type", "op": ":=", "value": _NAS_PORT_TYPE_ETHERNET},
    ]
    if profile:
        if profile.ip_pool_name:
            attrs.append({"attribute": "Framed-Pool", "op": ":=", "value": profile.ip_pool_name})
        if profile.vlan_id:
            attrs.append({"attribute": "Tunnel-Type", "op": ":=", "value": "VLAN"})
            attrs.append({"attribute": "Tunnel-Medium-Type", "op": ":=", "value": "IEEE-802"})
            attrs.append({"attribute": "Tunnel-Private-Group-Id", "op": ":=", "value": str(profile.vlan_id)})
        if profile.inner_vlan_id:
            attrs.append({"attribute": "Tunnel-Private-Group-Id", "op": "+=", "value": str(profile.inner_vlan_id)})
        if profile.session_timeout:
            attrs.append({"attribute": "Session-Timeout", "op": ":=", "value": str(profile.session_timeout)})
    if subscription.ipv4_address:
        attrs.append({"attribute": "Framed-IP-Address", "op": ":=", "value": subscription.ipv4_address})
    if subscription.mac_address:
        attrs.append({"attribute": "Calling-Station-Id", "op": ":=", "value": subscription.mac_address})
    return attrs


def _base_static_attributes(
    profile: RadiusProfile | None,
    subscription: Subscription,
) -> list[dict[str, str]]:
    """Generate RADIUS reply attributes for static IP connections."""
    attrs: list[dict[str, str]] = [
        {"attribute": "Service-Type", "op": ":=", "value": _SERVICE_TYPE_FRAMED},
    ]
    if subscription.ipv4_address:
        attrs.append({"attribute": "Framed-IP-Address", "op": ":=", "value": subscription.ipv4_address})
    if subscription.ipv6_address:
        attrs.append({"attribute": "Framed-IPv6-Prefix", "op": ":=", "value": subscription.ipv6_address})
    if profile:
        if profile.session_timeout:
            attrs.append({"attribute": "Session-Timeout", "op": ":=", "value": str(profile.session_timeout)})
    return attrs


def _base_hotspot_attributes(
    profile: RadiusProfile | None,
    subscription: Subscription,
) -> list[dict[str, str]]:
    """Generate RADIUS reply attributes for hotspot connections.

    Hotspot connections use Service-Type=Login and include
    MikroTik-specific hotspot attributes for bandwidth limiting
    and session management.
    """
    attrs: list[dict[str, str]] = [
        {"attribute": "Service-Type", "op": ":=", "value": _SERVICE_TYPE_LOGIN},
    ]
    if profile:
        if profile.ip_pool_name:
            attrs.append({"attribute": "Framed-Pool", "op": ":=", "value": profile.ip_pool_name})
        if profile.session_timeout:
            attrs.append({"attribute": "Session-Timeout", "op": ":=", "value": str(profile.session_timeout)})
        if profile.idle_timeout:
            attrs.append({"attribute": "Idle-Timeout", "op": ":=", "value": str(profile.idle_timeout)})
        if profile.simultaneous_use:
            attrs.append({"attribute": "Simultaneous-Use", "op": ":=", "value": str(profile.simultaneous_use)})
        # MikroTik hotspot-specific: advertise the profile name as the group
        if profile.name:
            attrs.append({"attribute": "Mikrotik-Group", "op": ":=", "value": profile.name})
    if subscription.ipv4_address:
        attrs.append({"attribute": "Framed-IP-Address", "op": ":=", "value": subscription.ipv4_address})
    if subscription.mac_address:
        attrs.append({"attribute": "Calling-Station-Id", "op": ":=", "value": subscription.mac_address})
    return attrs


# Map connection types to their base attribute generators
_CONNECTION_TYPE_ATTRS = {
    ConnectionType.pppoe: _base_pppoe_attributes,
    ConnectionType.dhcp: _base_dhcp_attributes,
    ConnectionType.ipoe: _base_ipoe_attributes,
    ConnectionType.static: _base_static_attributes,
    ConnectionType.hotspot: _base_hotspot_attributes,
}


def _append_vendor_attributes(
    attrs: list[dict[str, str]],
    profile: RadiusProfile | None,
    connection_type: ConnectionType,
) -> None:
    """Append vendor-specific RADIUS attributes (MikroTik, Huawei, etc.)."""
    if not profile:
        return
    # MikroTik Rate-Limit (applies to PPPoE and hotspot mainly)
    if connection_type in (ConnectionType.pppoe, ConnectionType.hotspot, ConnectionType.ipoe):
        from app.services.enforcement import _build_mikrotik_rate_limit
        rate_limit = _build_mikrotik_rate_limit(profile)
        if rate_limit:
            attrs.append({"attribute": "Mikrotik-Rate-Limit", "op": ":=", "value": rate_limit})

    if profile.mikrotik_address_list:
        attrs.append({"attribute": "Mikrotik-Address-List", "op": ":=", "value": profile.mikrotik_address_list})

    # VLAN attributes for PPPoE (if specified on profile)
    if connection_type == ConnectionType.pppoe and profile.vlan_id:
        attrs.append({"attribute": "Tunnel-Type", "op": ":=", "value": "VLAN"})
        attrs.append({"attribute": "Tunnel-Medium-Type", "op": ":=", "value": "IEEE-802"})
        attrs.append({"attribute": "Tunnel-Private-Group-Id", "op": ":=", "value": str(profile.vlan_id)})


# ---------------------------------------------------------------------------
# Connection type resolution
# ---------------------------------------------------------------------------

def resolve_connection_type(
    db: Session,
    subscription: Subscription,
    nas_device: NasDevice | None = None,
) -> ConnectionType:
    """Resolve the effective connection type for a subscription.

    Resolution order:
    1. RADIUS profile's explicit connection_type
    2. NAS connection rule matching the subscription
    3. NAS device's default_connection_type
    4. Fallback to PPPoE
    """
    # 1. Check profile-level connection type
    if subscription.radius_profile_id:
        profile = db.get(RadiusProfile, subscription.radius_profile_id)
        if profile and profile.connection_type:
            return profile.connection_type

    # 2. Resolve NAS device
    if not nas_device and subscription.provisioning_nas_device_id:
        nas_device = db.get(NasDevice, subscription.provisioning_nas_device_id)

    if nas_device:
        # 3. Check NAS connection rules (ordered by priority)
        rules = (
            db.query(NasConnectionRule)
            .filter(NasConnectionRule.nas_device_id == nas_device.id)
            .filter(NasConnectionRule.is_active.is_(True))
            .order_by(NasConnectionRule.priority.asc())
            .all()
        )
        for rule in rules:
            if _rule_matches(rule, subscription):
                if rule.connection_type:
                    return rule.connection_type

        # 4. NAS device default
        if nas_device.default_connection_type:
            return nas_device.default_connection_type

    return ConnectionType.pppoe


def _rule_matches(rule: NasConnectionRule, subscription: Subscription) -> bool:
    """Check if a NAS connection rule matches a subscription.

    match_expression supports simple patterns:
    - "*" matches everything
    - "login:prefix_*" matches login starting with prefix_
    - "mac:AA:BB:*" matches MAC prefix
    """
    if not rule.match_expression:
        return True  # No expression = matches all
    expr = rule.match_expression.strip()
    if expr == "*":
        return True
    if expr.startswith("login:") and subscription.login:
        pattern = expr[6:]
        if pattern.endswith("*"):
            return subscription.login.startswith(pattern[:-1])
        return subscription.login == pattern
    if expr.startswith("mac:") and subscription.mac_address:
        pattern = expr[4:]
        if pattern.endswith("*"):
            return subscription.mac_address.upper().startswith(pattern[:-1].upper())
        return subscription.mac_address.upper() == pattern.upper()
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _append_option82_attributes(
    db: Session,
    attrs: list[dict[str, str]],
    subscription: Subscription,
    connection_type: ConnectionType,
) -> None:
    """Append DHCP Option 82 relay agent attributes for IPoE connections.

    Looks up the subscriber's access credentials for circuit-id and
    remote-id fields, then generates the corresponding RADIUS check
    attributes for relay agent matching.
    """
    if connection_type != ConnectionType.ipoe:
        return

    from app.models.catalog import AccessCredential
    credential = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscription.subscriber_id)
        .filter(AccessCredential.is_active.is_(True))
        .filter(
            (AccessCredential.circuit_id.isnot(None))
            | (AccessCredential.remote_id.isnot(None))
        )
        .first()
    )
    if not credential:
        return
    if credential.circuit_id:
        attrs.append({
            "attribute": "Agent-Circuit-Id",
            "op": ":=",
            "value": credential.circuit_id,
        })
    if credential.remote_id:
        attrs.append({
            "attribute": "Agent-Remote-Id",
            "op": ":=",
            "value": credential.remote_id,
        })


def build_radius_reply_attributes(
    db: Session,
    subscription: Subscription,
    profile: RadiusProfile | None = None,
    nas_device: NasDevice | None = None,
) -> list[dict[str, str]]:
    """Build the full set of RADIUS reply attributes for a subscription.

    Generates connection-type-specific base attributes, then appends
    vendor-specific, Option 82, and profile-level custom attributes.

    Args:
        db: Database session
        subscription: The subscription to build attributes for
        profile: Optional explicit profile (resolved automatically if None)
        nas_device: Optional NAS device (resolved from subscription if None)

    Returns:
        List of dicts with keys: attribute, op, value
    """
    if not profile and subscription.radius_profile_id:
        profile = db.get(RadiusProfile, subscription.radius_profile_id)

    connection_type = resolve_connection_type(db, subscription, nas_device)

    # Get base attributes for this connection type
    attr_fn = _CONNECTION_TYPE_ATTRS.get(connection_type, _base_pppoe_attributes)
    attrs = attr_fn(profile, subscription)

    # Append vendor-specific attributes
    _append_vendor_attributes(attrs, profile, connection_type)

    # Append Option 82 relay agent attributes for IPoE
    _append_option82_attributes(db, attrs, subscription, connection_type)

    # Append any custom attributes from the profile
    if profile:
        from app.models.catalog import RadiusAttribute
        custom_attrs = (
            db.query(RadiusAttribute)
            .filter(RadiusAttribute.profile_id == profile.id)
            .all()
        )
        seen = {a["attribute"].lower() for a in attrs}
        for attr in custom_attrs:
            if attr.attribute.lower() not in seen:
                attrs.append({
                    "attribute": attr.attribute,
                    "op": attr.operator or ":=",
                    "value": attr.value,
                })
                seen.add(attr.attribute.lower())

    logger.debug(
        "Built %d RADIUS attributes for subscription %s (type=%s).",
        len(attrs), subscription.id, connection_type.value,
    )
    return attrs


def build_nas_provisioning_commands(
    db: Session,
    subscription: Subscription,
    nas_device: NasDevice,
    profile: RadiusProfile | None = None,
    action: str = "create",
) -> list[str]:
    """Build NAS CLI commands for provisioning a subscription.

    Generates vendor-specific commands based on connection type.

    Args:
        db: Database session
        subscription: The subscription to provision
        nas_device: Target NAS device
        profile: RADIUS profile (resolved if None)
        action: create, delete, suspend, unsuspend

    Returns:
        List of CLI command strings
    """
    from app.models.catalog import NasVendor

    if not profile and subscription.radius_profile_id:
        profile = db.get(RadiusProfile, subscription.radius_profile_id)

    connection_type = resolve_connection_type(db, subscription, nas_device)
    commands: list[str] = []

    if nas_device.vendor == NasVendor.mikrotik:
        commands = _mikrotik_commands(subscription, profile, connection_type, action)
    else:
        # Generic / other vendors: log and return empty (use provisioning templates)
        logger.debug(
            "No built-in commands for vendor %s, use provisioning templates.",
            nas_device.vendor.value,
        )

    return commands


def _mikrotik_commands(
    subscription: Subscription,
    profile: RadiusProfile | None,
    connection_type: ConnectionType,
    action: str,
) -> list[str]:
    """Generate MikroTik RouterOS commands for provisioning."""
    from app.services.enforcement import _sanitize_routeros_value

    login = _sanitize_routeros_value(subscription.login or "")
    ip = _sanitize_routeros_value(subscription.ipv4_address or "")
    mac = _sanitize_routeros_value(subscription.mac_address or "")
    prof_name = _sanitize_routeros_value(profile.name if profile else "")
    commands: list[str] = []

    if connection_type == ConnectionType.pppoe:
        if action == "create":
            parts = [f'/ppp secret add name="{login}"']
            if prof_name:
                parts.append(f'profile="{prof_name}"')
            if ip:
                parts.append(f'remote-address={ip}')
            parts.append('service=pppoe')
            commands.append(" ".join(parts))
        elif action == "delete":
            commands.append(f'/ppp secret remove [find name="{login}"]')
        elif action == "suspend":
            commands.append(f'/ppp secret set [find name="{login}"] disabled=yes')
            commands.append(f'/ppp active remove [find name="{login}"]')
        elif action == "unsuspend":
            commands.append(f'/ppp secret set [find name="{login}"] disabled=no')

    elif connection_type == ConnectionType.dhcp:
        if action == "create" and ip:
            parts = [f'/ip dhcp-server lease add address={ip}']
            if mac:
                parts.append(f'mac-address={mac}')
            if profile and profile.mikrotik_rate_limit:
                from app.services.enforcement import _build_mikrotik_rate_limit
                rate = _build_mikrotik_rate_limit(profile)
                if rate:
                    parts.append(f'rate-limit={_sanitize_routeros_value(rate)}')
            commands.append(" ".join(parts))
        elif action == "delete" and ip:
            commands.append(f'/ip dhcp-server lease remove [find address={ip}]')
        elif action == "suspend" and ip:
            commands.append(f'/ip dhcp-server lease set [find address={ip}] disabled=yes')
        elif action == "unsuspend" and ip:
            commands.append(f'/ip dhcp-server lease set [find address={ip}] disabled=no')

    elif connection_type == ConnectionType.hotspot:
        if action == "create":
            parts = [f'/ip hotspot user add name="{login}"']
            if prof_name:
                parts.append(f'profile="{prof_name}"')
            if ip:
                parts.append(f'address={ip}')
            commands.append(" ".join(parts))
        elif action == "delete":
            commands.append(f'/ip hotspot user remove [find name="{login}"]')
        elif action == "suspend":
            commands.append(f'/ip hotspot user set [find name="{login}"] disabled=yes')
            commands.append(f'/ip hotspot active remove [find user="{login}"]')
        elif action == "unsuspend":
            commands.append(f'/ip hotspot user set [find name="{login}"] disabled=no')

    elif connection_type == ConnectionType.static:
        if action == "suspend" and ip:
            commands.append(
                f'/ip firewall address-list add list=blocked-subscribers address={ip}'
            )
        elif action == "unsuspend" and ip:
            commands.append(
                f'/ip firewall address-list remove [find list=blocked-subscribers address={ip}]'
            )

    elif connection_type == ConnectionType.ipoe:
        if action == "create" and ip:
            parts = [f'/ip dhcp-server lease add address={ip}']
            if mac:
                parts.append(f'mac-address={mac}')
            parts.append('use-src-mac=yes')
            commands.append(" ".join(parts))
        elif action == "delete" and ip:
            commands.append(f'/ip dhcp-server lease remove [find address={ip}]')

    return commands
