"""Validation helpers for ACS management reachability.

These checks keep CRUD-managed OLT config packs from declaring ACS support
without a routable management VLAN and IP pool.
"""

from __future__ import annotations

from ipaddress import IPv4Network, ip_address, ip_network
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.network import IpBlock, IpPool, IPVersion, OLTDevice, Vlan


def _parse_ipv4_network(value: Any) -> IPv4Network | None:
    try:
        network = ip_network(str(value or "").strip(), strict=False)
    except ValueError:
        return None
    return network if isinstance(network, IPv4Network) else None


def _configured_routable_cidrs() -> list[IPv4Network]:
    networks: list[IPv4Network] = []
    for raw in str(settings.acs_routable_management_cidrs or "").split(","):
        network = _parse_ipv4_network(raw)
        if network is not None:
            networks.append(network)
    return networks


def _pool_networks(db: Session, pool: IpPool) -> list[IPv4Network]:
    blocks = list(
        db.scalars(
            select(IpBlock)
            .where(IpBlock.pool_id == pool.id)
            .where(IpBlock.is_active.is_(True))
        ).all()
    )
    if blocks:
        return [
            network
            for block in blocks
            if (network := _parse_ipv4_network(block.cidr)) is not None
        ]
    network = _parse_ipv4_network(pool.cidr)
    return [network] if network is not None else []


def _ensure_pool_has_active_block(db: Session, pool: IpPool) -> None:
    """Make the pool allocator contract match the pool CIDR."""
    active_block = db.scalars(
        select(IpBlock)
        .where(IpBlock.pool_id == pool.id)
        .where(IpBlock.is_active.is_(True))
    ).first()
    if active_block is not None:
        return

    network = _parse_ipv4_network(pool.cidr)
    if network is None:
        return

    existing_block = db.scalars(
        select(IpBlock)
        .where(IpBlock.pool_id == pool.id)
        .where(IpBlock.cidr == str(network))
    ).first()
    if existing_block is not None:
        existing_block.is_active = True
    else:
        db.add(IpBlock(pool_id=pool.id, cidr=str(network), is_active=True))
    db.flush()


def _networks_within(candidates: list[IPv4Network], allowed: list[IPv4Network]) -> bool:
    return bool(candidates) and all(
        any(candidate.subnet_of(parent) for parent in allowed)
        for candidate in candidates
    )


def _gateway_in_pool(pool: IpPool, networks: list[IPv4Network]) -> bool:
    gateway = str(getattr(pool, "gateway", "") or "").strip()
    if not gateway:
        return False
    try:
        gateway_ip = ip_address(gateway)
    except ValueError:
        return False
    return any(gateway_ip in network for network in networks)


def validate_olt_acs_management_reachability(
    db: Session,
    values: dict[str, object],
    *,
    current_olt: OLTDevice | None = None,
) -> str | None:
    """Return an error message when ACS management reachability is invalid."""
    acs_server_id = values.get("tr069_acs_server_id") or (
        getattr(current_olt, "tr069_acs_server_id", None) if current_olt else None
    )
    tr069_profile_id = values.get("default_tr069_olt_profile_id")
    acs_enabled = bool(acs_server_id or tr069_profile_id)
    if not acs_enabled:
        return None

    management_vlan_id = values.get("management_vlan_id")
    if not management_vlan_id:
        return "ACS-managed OLTs require a management VLAN in the config pack."

    mgmt_ip_pool_id = values.get("mgmt_ip_pool_id")
    if not mgmt_ip_pool_id:
        return "ACS-managed OLTs require a management IP pool."

    vlan = db.get(Vlan, management_vlan_id)
    if vlan is None or not vlan.is_active:
        return "Management VLAN must exist and be active."

    pool = db.get(IpPool, mgmt_ip_pool_id)
    if pool is None or not pool.is_active:
        return "Management IP pool must exist and be active."
    if pool.ip_version != IPVersion.ipv4:
        return "ACS management IP pool must be IPv4."
    _ensure_pool_has_active_block(db, pool)

    olt_id = getattr(current_olt, "id", None)
    pool_olt_id = getattr(pool, "olt_device_id", None)
    vlan_olt_id = getattr(vlan, "olt_device_id", None)
    if olt_id is not None:
        if pool_olt_id and pool_olt_id != olt_id:
            return "Management IP pool belongs to a different OLT."
        if vlan_olt_id and vlan_olt_id != olt_id:
            return "Management VLAN belongs to a different OLT."
    if pool.vlan_id and pool.vlan_id != vlan.id:
        return (
            "Management IP pool must be associated with the selected management VLAN."
        )

    pool_networks = _pool_networks(db, pool)
    if not pool_networks:
        return "Management IP pool must have at least one valid IPv4 CIDR block."
    if not _gateway_in_pool(pool, pool_networks):
        return "Management IP pool gateway must be set and inside the pool CIDR."

    allowed_networks = _configured_routable_cidrs()
    if not allowed_networks:
        return "ACS_ROUTABLE_MANAGEMENT_CIDRS must list the management CIDRs reachable from GenieACS."
    if not _networks_within(pool_networks, allowed_networks):
        allowed = ", ".join(str(network) for network in allowed_networks)
        pool_cidrs = ", ".join(str(network) for network in pool_networks)
        return (
            "Management IP pool CIDRs must be routable from GenieACS. "
            f"Pool: {pool_cidrs}; allowed: {allowed}."
        )

    from app.services.network.ont_management_ipam import refresh_pool_availability

    next_ip, available_count = refresh_pool_availability(db, pool.id)
    if not next_ip or available_count < 1:
        return "Management IP pool must have at least one available address."

    return None
