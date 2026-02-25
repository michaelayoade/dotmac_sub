"""Service helpers for admin network IP-management web routes."""

from __future__ import annotations

import ipaddress
from collections import defaultdict

from pydantic import ValidationError

from app.models.network import IPv4Address, IPv6Address
from app.schemas.network import IpBlockCreate, IpPoolCreate, IpPoolUpdate
from app.services import network as network_service
from app.services.audit_helpers import diff_dicts, model_to_dict
from app.services.common import coerce_uuid, validate_enum


def _usable_ipv4_count(cidr: str) -> int:
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return 0
    if network.version != 4:
        return 0
    if network.prefixlen >= 31:
        return int(network.num_addresses)
    return max(0, int(network.num_addresses) - 2)


def build_ip_management_data(db) -> dict[str, object]:
    pools = network_service.ip_pools.list(
        db=db,
        ip_version=None,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    blocks = network_service.ip_blocks.list(
        db=db,
        pool_id=None,
        is_active=None,
        order_by="cidr",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    assignments = network_service.ip_assignments.list(
        db=db,
        subscriber_id=None,
        subscription_id=None,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )
    ipv4_addresses = network_service.ipv4_addresses.list(
        db=db,
        pool_id=None,
        is_reserved=None,
        order_by="address",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    ipv6_addresses = network_service.ipv6_addresses.list(
        db=db,
        pool_id=None,
        is_reserved=None,
        order_by="address",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    stats = {
        "total_pools": len(pools),
        "total_blocks": len(blocks),
        "total_assignments": len(assignments),
        "total_addresses": len(ipv4_addresses) + len(ipv6_addresses),
    }
    return {
        "pools": pools,
        "blocks": blocks,
        "assignments": assignments,
        "ipv4_addresses": ipv4_addresses,
        "ipv6_addresses": ipv6_addresses,
        "stats": stats,
    }


def get_ip_pool_new_form_data() -> dict[str, object]:
    return {
        "pool": None,
        "action_url": "/admin/network/ip-management/pools",
    }


def get_ip_block_new_form_data(db, *, pool_id: str | None = None) -> dict[str, object]:
    pools = list_active_ip_pools(db)
    return {
        "block": {"pool_id": pool_id} if pool_id else None,
        "pools": pools,
        "action_url": "/admin/network/ip-management/blocks",
    }


def list_active_ip_pools(db):
    return network_service.ip_pools.list(
        db=db,
        ip_version=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )


def parse_ip_block_form(form) -> dict[str, object]:
    return {
        "pool_id": form.get("pool_id", "").strip(),
        "cidr": form.get("cidr", "").strip(),
        "notes": form.get("notes", "").strip() or None,
        "is_active": form.get("is_active") == "true",
    }


def validate_ip_block_values(values: dict[str, object]) -> str | None:
    if not values.get("pool_id"):
        return "IP pool is required."
    if not values.get("cidr"):
        return "CIDR block is required."
    return None


def create_ip_block(db, values: dict[str, object]):
    try:
        normalized = dict(values)
        normalized["pool_id"] = coerce_uuid(str(values.get("pool_id") or ""))
        payload = IpBlockCreate.model_validate(normalized)
        block = network_service.ip_blocks.create(db=db, payload=payload)
        return block, None
    except ValidationError as exc:
        return None, exc.errors()[0]["msg"]
    except Exception as exc:
        return None, str(exc)


def parse_ip_pool_form(form) -> dict[str, object]:
    return {
        "name": form.get("name", "").strip(),
        "ip_version": form.get("ip_version", "").strip(),
        "cidr": form.get("cidr", "").strip(),
        "gateway": form.get("gateway", "").strip() or None,
        "dns_primary": form.get("dns_primary", "").strip() or None,
        "dns_secondary": form.get("dns_secondary", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
        "is_active": form.get("is_active") == "true",
    }


def validate_ip_pool_values(values: dict[str, object]) -> str | None:
    if not values.get("name"):
        return "Pool name is required."
    if not values.get("ip_version"):
        return "IP version is required."
    if not values.get("cidr"):
        return "CIDR block is required."
    return None


def pool_form_snapshot(values: dict[str, object], *, pool_id: str | None = None) -> dict[str, object]:
    data = {
        "name": values.get("name"),
        "ip_version": {"value": values.get("ip_version")},
        "cidr": values.get("cidr"),
        "gateway": values.get("gateway"),
        "dns_primary": values.get("dns_primary"),
        "dns_secondary": values.get("dns_secondary"),
        "notes": values.get("notes"),
        "is_active": values.get("is_active"),
    }
    if pool_id:
        data["id"] = pool_id
    return data


def create_ip_pool(db, values: dict[str, object]):
    try:
        from app.models.network import IPVersion

        normalized = dict(values)
        normalized["ip_version"] = validate_enum(
            str(values.get("ip_version") or ""), IPVersion, "ip_version"
        )
        payload = IpPoolCreate.model_validate(normalized)
        pool = network_service.ip_pools.create(db=db, payload=payload)
        return pool, None
    except ValidationError as exc:
        return None, exc.errors()[0]["msg"]
    except Exception as exc:
        return None, str(exc)


def update_ip_pool(db, *, pool_id: str, values: dict[str, object]):
    try:
        current = network_service.ip_pools.get(db=db, pool_id=pool_id)
    except Exception:
        return None, None, "IP Pool not found"

    before_snapshot = model_to_dict(current)
    try:
        from app.models.network import IPVersion

        normalized = dict(values)
        if values.get("ip_version"):
            normalized["ip_version"] = validate_enum(
                str(values.get("ip_version") or ""), IPVersion, "ip_version"
            )
        payload = IpPoolUpdate.model_validate(normalized)
        network_service.ip_pools.update(db=db, pool_id=pool_id, payload=payload)
        after = network_service.ip_pools.get(db=db, pool_id=pool_id)
        after_snapshot = model_to_dict(after)
        changes = diff_dicts(before_snapshot, after_snapshot)
        return after, changes, None
    except ValidationError as exc:
        return None, None, exc.errors()[0]["msg"]
    except Exception as exc:
        return None, None, str(exc)


def build_ip_pool_detail_data(db, *, pool_id: str) -> dict[str, object] | None:
    try:
        pool = network_service.ip_pools.get(db=db, pool_id=pool_id)
    except Exception:
        return None

    blocks = network_service.ip_blocks.list(
        db=db,
        pool_id=pool_id,
        is_active=None,
        order_by="cidr",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    if pool.ip_version.value == "ipv4":
        assignments = (
            db.query(IPv4Address).filter(IPv4Address.pool_id == pool.id).limit(100).all()
        )
    else:
        assignments = (
            db.query(IPv6Address).filter(IPv6Address.pool_id == pool.id).limit(100).all()
        )
    return {
        "pool": pool,
        "blocks": blocks,
        "assignments": assignments,
    }


def get_ip_pool_for_edit(db, *, pool_id: str):
    try:
        return network_service.ip_pools.get(db=db, pool_id=pool_id)
    except Exception:
        return None


def build_ip_assignments_data(db) -> dict[str, object]:
    assignments = network_service.ip_assignments.list(
        db=db,
        subscriber_id=None,
        subscription_id=None,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )
    return {
        "assignments": assignments,
        "stats": {
            "total": len(assignments),
            "active": sum(1 for a in assignments if a.is_active),
        },
    }


def build_ip_addresses_data(db, *, ip_version: str) -> dict[str, object]:
    if ip_version == "ipv4":
        addresses = network_service.ipv4_addresses.list(
            db=db,
            pool_id=None,
            is_reserved=None,
            order_by="address",
            order_dir="asc",
            limit=200,
            offset=0,
        )
    else:
        addresses = network_service.ipv6_addresses.list(
            db=db,
            pool_id=None,
            is_reserved=None,
            order_by="address",
            order_dir="asc",
            limit=200,
            offset=0,
        )
    pools = network_service.ip_pools.list(
        db=db,
        ip_version=ip_version,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    stats = {
        "total": len(addresses),
        "reserved": sum(1 for a in addresses if a.is_reserved),
        "available": sum(1 for a in addresses if not a.is_reserved),
    }
    return {
        "addresses": addresses,
        "pools": pools,
        "stats": stats,
        "ip_version": ip_version,
    }


def build_ip_pools_data(db) -> dict[str, object]:
    pools = network_service.ip_pools.list(
        db=db,
        ip_version=None,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    blocks = network_service.ip_blocks.list(
        db=db,
        pool_id=None,
        is_active=True,
        order_by="cidr",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    pool_ids = [str(pool.id) for pool in pools if pool.ip_version.value == "ipv4"]
    ipv4_records = network_service.ipv4_addresses.list(
        db=db,
        pool_id=None,
        is_reserved=None,
        order_by="address",
        order_dir="asc",
        limit=50000,
        offset=0,
    )
    ipv4_by_pool: dict[str, list[str]] = defaultdict(list)
    for record in ipv4_records:
        if record.pool_id and str(record.pool_id) in pool_ids:
            ipv4_by_pool[str(record.pool_id)].append(str(record.address))

    pool_utilization: dict[str, dict[str, int]] = {}
    for pool in pools:
        pool_id = str(pool.id)
        if pool.ip_version.value != "ipv4":
            pool_utilization[pool_id] = {"used": 0, "total": 0, "percent": 0}
            continue
        total = _usable_ipv4_count(str(pool.cidr))
        used = len(ipv4_by_pool.get(pool_id, []))
        percent = int(round((used / total) * 100)) if total > 0 else 0
        pool_utilization[pool_id] = {
            "used": used,
            "total": total,
            "percent": max(0, min(percent, 100)),
        }

    block_utilization: dict[str, dict[str, int]] = {}
    for block in blocks:
        block_id = str(block.id)
        pool = getattr(block, "pool", None)
        pool_id = str(pool.id) if pool else None
        if not pool_id or not pool or pool.ip_version.value != "ipv4":
            block_utilization[block_id] = {"used": 0, "total": 0, "percent": 0}
            continue
        try:
            network = ipaddress.ip_network(str(block.cidr), strict=False)
        except ValueError:
            block_utilization[block_id] = {"used": 0, "total": 0, "percent": 0}
            continue
        used = 0
        for address in ipv4_by_pool.get(pool_id, []):
            try:
                if ipaddress.ip_address(address) in network:
                    used += 1
            except ValueError:
                continue
        total = _usable_ipv4_count(str(block.cidr))
        percent = int(round((used / total) * 100)) if total > 0 else 0
        block_utilization[block_id] = {
            "used": used,
            "total": total,
            "percent": max(0, min(percent, 100)),
        }

    stats = {
        "total_pools": len(pools),
        "total_blocks": len(blocks),
        "ipv4_pools": sum(1 for p in pools if p.ip_version.value == "ipv4"),
        "ipv6_pools": sum(1 for p in pools if p.ip_version.value == "ipv6"),
    }
    return {
        "pools": pools,
        "blocks": blocks,
        "pool_utilization": pool_utilization,
        "block_utilization": block_utilization,
        "stats": stats,
    }
