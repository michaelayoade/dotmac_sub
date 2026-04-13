"""Service helpers for admin network IP-management web routes."""

from __future__ import annotations

import csv
import io
import ipaddress
import logging
import re
from collections import defaultdict
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import joinedload

from app.models.network import IPAssignment, IPv4Address, IPv6Address, IPVersion
from app.schemas.catalog import SubscriptionUpdate
from app.schemas.network import (
    IPAssignmentCreate,
    IPAssignmentUpdate,
    IpBlockCreate,
    IpPoolCreate,
    IpPoolUpdate,
)
from app.services import catalog as catalog_service
from app.services import network as network_service
from app.services.audit_helpers import diff_dicts, model_to_dict
from app.services.common import coerce_uuid, validate_enum

logger = logging.getLogger(__name__)

_FALLBACK_MARKER = "[fallback]"
_POOL_META_KEYS = (
    "location",
    "category",
    "network_type",
    "router",
    "usage_type",
    "allow_network_broadcast",
)


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


def _ipv6_capacity_count(cidr: str) -> int:
    """Return IPv6 pool capacity count for utilization.

    For prefixes up to /64, capacity is tracked as number of /64 delegations.
    For prefixes longer than /64, capacity is tracked as raw address count.
    """
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return 0
    if network.version != 6:
        return 0
    if network.prefixlen <= 64:
        return int(1 << (64 - network.prefixlen))
    return int(1 << (128 - network.prefixlen))


def _ipv4_capacity_count(cidr: str, *, allow_network_broadcast: bool = False) -> int:
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return 0
    if network.version != 4:
        return 0
    if allow_network_broadcast or network.prefixlen >= 31:
        return int(network.num_addresses)
    return max(0, int(network.num_addresses) - 2)


def _parse_network(cidr: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None


def _active_assignment(record) -> IPAssignment | None:
    assignment = getattr(record, "assignment", None)
    if assignment and getattr(assignment, "is_active", False):
        return assignment
    return None


def _query_count(db, model, where_clause=None) -> int:
    from sqlalchemy import func, select

    if hasattr(db, "execute"):
        stmt = select(func.count(model.id))
        if where_clause is not None:
            stmt = stmt.where(where_clause)
        return int(db.execute(stmt).scalar() or 0)
    query = db.query(model)
    if where_clause is not None and hasattr(query, "filter"):
        query = query.filter(where_clause)
    rows = query.all() if hasattr(query, "all") else []
    return len(rows)


def _subscriber_display_name(subscriber) -> str | None:
    if not subscriber:
        return None
    return (
        str(getattr(subscriber, "display_name", "") or "").strip()
        or str(getattr(subscriber, "full_name", "") or "").strip()
        or (
            f"{str(getattr(subscriber, 'first_name', '') or '').strip()} "
            f"{str(getattr(subscriber, 'last_name', '') or '').strip()}"
        ).strip()
        or str(getattr(subscriber, "email", "") or "").strip()
        or str(getattr(subscriber, "subscriber_number", "") or "").strip()
        or str(getattr(subscriber, "id", "") or "").strip()
        or None
    )


def _subscription_display_name(subscription) -> str | None:
    if not subscription:
        return None
    return (
        str(getattr(subscription, "service_id", "") or "").strip()
        or str(getattr(subscription, "id", "") or "").strip()
        or None
    )


def _pool_metadata(pool) -> tuple[dict[str, str | None], str | None]:
    return parse_pool_notes_metadata(getattr(pool, "notes", None))


def _pool_allows_network_broadcast(pool) -> bool:
    metadata, _ = _pool_metadata(pool)
    return str(metadata.get("allow_network_broadcast") or "").lower() == "true"


def _ip_version_value(value) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value or "")


def _prefix_from_bm(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        prefix = int(raw)
        if 0 <= prefix <= 32:
            return str(prefix)
        return None
    try:
        network = ipaddress.ip_network(f"0.0.0.0/{raw}", strict=False)
    except ValueError:
        return None
    return str(network.prefixlen)


def _overlapping_pool_error(
    db,
    *,
    cidr: str,
    ip_version_value: str | None,
    exclude_pool_id: str | None = None,
) -> str | None:
    target_network = _parse_network(cidr)
    if target_network is None:
        return None

    if ip_version_value in {"ipv4", "ipv6"}:
        version_filter = ip_version_value
    else:
        version_filter = "ipv4" if target_network.version == 4 else "ipv6"

    pools = network_service.ip_pools.list(
        db=db,
        ip_version=version_filter,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=5000,
        offset=0,
    )
    for pool in pools:
        if exclude_pool_id and str(pool.id) == exclude_pool_id:
            continue
        other_network = _parse_network(pool.cidr)
        if other_network is None:
            continue
        if other_network.version != target_network.version:
            continue
        if target_network.overlaps(other_network):
            return f"CIDR {cidr} overlaps existing pool {pool.name} ({pool.cidr})."
    return None


def _normalize_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def is_fallback_pool_notes(notes: str | None) -> bool:
    return _FALLBACK_MARKER in str(notes or "").lower()


def parse_pool_notes_metadata(
    notes: str | None,
) -> tuple[dict[str, str | None], str | None]:
    text = str(notes or "").strip()
    metadata: dict[str, str | None] = dict.fromkeys(_POOL_META_KEYS)
    if not text:
        return metadata, None

    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        matched = False
        lowered = stripped.lower()
        for key in _POOL_META_KEYS:
            prefix = f"[{key}:"
            if lowered.startswith(prefix) and lowered.endswith("]"):
                value = stripped[len(prefix) : -1].strip()
                metadata[key] = value or None
                matched = True
                break
        if not matched:
            cleaned_lines.append(stripped)
    cleaned_text = "\n".join(line for line in cleaned_lines if line).strip() or None
    return metadata, cleaned_text


def _strip_fallback_marker(notes: str | None) -> str | None:
    text = str(notes or "").strip()
    if not text:
        return None
    cleaned = re.sub(r"\[fallback\]", "", text, flags=re.IGNORECASE)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return None
    return "\n".join(lines)


def normalize_pool_notes(
    *,
    notes: str | None,
    is_fallback: bool,
    location: str | None = None,
    category: str | None = None,
    network_type: str | None = None,
    router: str | None = None,
    usage_type: str | None = None,
    allow_network_broadcast: bool | None = None,
) -> str | None:
    base = _strip_fallback_marker(notes)
    _, base_without_meta = parse_pool_notes_metadata(base)
    lines: list[str] = []
    if is_fallback:
        lines.append(_FALLBACK_MARKER)
    meta_values = {
        "location": str(location or "").strip() or None,
        "category": str(category or "").strip() or None,
        "network_type": str(network_type or "").strip() or None,
        "router": str(router or "").strip() or None,
        "usage_type": str(usage_type or "").strip() or None,
        "allow_network_broadcast": (
            "true"
            if allow_network_broadcast is True
            else ("false" if allow_network_broadcast is False else None)
        ),
    }
    for key in _POOL_META_KEYS:
        value = meta_values.get(key)
        if value:
            lines.append(f"[{key}:{value}]")
    if base_without_meta:
        lines.append(base_without_meta)
    return "\n".join(lines).strip() or None


def parse_ip_pool_csv(csv_text: str) -> list[dict[str, str]]:
    """Parse CSV for bulk pool import.

    Supported headers: name,cidr,ip_version,gateway,dns_primary,dns_secondary,notes,
    is_active,is_fallback,location,category,network_type,router
    """
    text = (csv_text or "").strip()
    if not text:
        return []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    normalized_rows: list[dict[str, str]] = []
    for raw in reader:
        row: dict[str, str] = {
            str(k or "").strip().lower(): str(v or "").strip() for k, v in raw.items()
        }
        normalized_rows.append(row)
    return normalized_rows


def _pool_payload_from_import_row(
    row: dict[str, str],
    *,
    default_ip_version: str,
    fallback_name: str,
) -> dict[str, object]:
    network_value = row.get("cidr") or row.get("network") or ""
    prefix_value = _prefix_from_bm(
        row.get("bm")
        or row.get("prefix")
        or row.get("prefix_length")
        or row.get("subnet_mask")
    )
    cidr = network_value
    if network_value and "/" not in network_value and prefix_value:
        cidr = f"{network_value}/{prefix_value}"

    inferred_version = default_ip_version
    parsed_network = _parse_network(cidr)
    if parsed_network is not None:
        inferred_version = "ipv4" if parsed_network.version == 4 else "ipv6"

    name = (
        row.get("name") or row.get("title") or row.get("description") or fallback_name
    )

    return {
        "name": name,
        "ip_version": row.get("ip_version", "") or inferred_version,
        "cidr": cidr,
        "gateway": row.get("gateway") or None,
        "dns_primary": row.get("dns_primary") or None,
        "dns_secondary": row.get("dns_secondary") or None,
        "olt_device_id": row.get("olt_device_id") or row.get("olt_id") or None,
        "vlan_id": row.get("vlan_id") or None,
        "notes": row.get("notes") or None,
        "location": row.get("location") or None,
        "category": row.get("category") or row.get("network category") or None,
        "network_type": row.get("network_type") or row.get("network type") or None,
        "router": row.get("router") or None,
        "usage_type": row.get("usage_type") or None,
        "allow_network_broadcast": (
            None
            if not str(row.get("allow_network_broadcast") or "").strip()
            else _normalize_bool(row.get("allow_network_broadcast"), False)
        ),
        "is_active": _normalize_bool(row.get("is_active"), True),
        "is_fallback": _normalize_bool(row.get("is_fallback"), False),
    }


def import_ip_pools_csv(
    db,
    *,
    csv_text: str,
    default_ip_version: str = "ipv4",
) -> dict[str, object]:
    rows = parse_ip_pool_csv(csv_text)
    created: list[object] = []
    errors: list[dict[str, object]] = []

    for index, row in enumerate(rows, start=2):  # header is line 1
        payload = _pool_payload_from_import_row(
            row,
            default_ip_version=default_ip_version,
            fallback_name=f"Imported {index}",
        )
        cidr = str(payload.get("cidr") or "")
        name = str(payload.get("name") or f"Imported {cidr or index}")
        validation_error = validate_ip_pool_values(payload)
        if validation_error:
            errors.append(
                {"line": index, "name": name, "cidr": cidr, "error": validation_error}
            )
            continue
        pool, error = create_ip_pool(db, payload)
        if error or pool is None:
            errors.append(
                {
                    "line": index,
                    "name": name,
                    "cidr": cidr,
                    "error": error or "Unknown error",
                }
            )
            continue
        created.append(pool)

    return {"created": created, "errors": errors, "total_rows": len(rows)}


def _build_pool_and_block_utilization(
    db,
    *,
    pools: list[object],
    blocks: list[object],
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    ipv4_pools = [
        pool
        for pool in pools
        if _ip_version_value(getattr(pool, "ip_version", None)) == "ipv4"
    ]
    ipv6_pools = [
        pool
        for pool in pools
        if _ip_version_value(getattr(pool, "ip_version", None)) == "ipv6"
    ]

    pool_utilization: dict[str, dict[str, int]] = {}
    block_utilization: dict[str, dict[str, int]] = {}

    ipv4_pool_ids = [pool.id for pool in ipv4_pools]
    ipv6_pool_ids = [pool.id for pool in ipv6_pools]

    ipv4_records = (
        db.query(IPv4Address)
        .options(joinedload(IPv4Address.assignment))
        .filter(IPv4Address.pool_id.in_(ipv4_pool_ids))
        .all()
        if ipv4_pool_ids
        else []
    )
    ipv6_records = (
        db.query(IPv6Address)
        .options(joinedload(IPv6Address.assignment))
        .filter(IPv6Address.pool_id.in_(ipv6_pool_ids))
        .all()
        if ipv6_pool_ids
        else []
    )

    ipv4_by_pool: dict[str, list[IPv4Address]] = defaultdict(list)
    for record in ipv4_records:
        pool_id = str(getattr(record, "pool_id", "") or "")
        if pool_id:
            ipv4_by_pool[pool_id].append(record)

    ipv6_by_pool: dict[str, list[IPv6Address]] = defaultdict(list)
    for record in ipv6_records:
        pool_id = str(getattr(record, "pool_id", "") or "")
        if pool_id:
            ipv6_by_pool[pool_id].append(record)

    for pool in pools:
        pool_id = str(pool.id)
        version = _ip_version_value(getattr(pool, "ip_version", None))
        if version == "ipv4":
            allow_network_broadcast = _pool_allows_network_broadcast(pool)
            total = _ipv4_capacity_count(
                str(pool.cidr), allow_network_broadcast=allow_network_broadcast
            )
            records = ipv4_by_pool.get(pool_id, [])
        else:
            total = _ipv6_capacity_count(str(pool.cidr))
            records = ipv6_by_pool.get(pool_id, [])
        used = sum(1 for record in records if _active_assignment(record))
        reserved = sum(
            1
            for record in records
            if getattr(record, "is_reserved", False) and not _active_assignment(record)
        )
        available = max(total - used - reserved, 0)
        percent = int(round((used / total) * 100)) if total > 0 else 0
        pool_utilization[pool_id] = {
            "used": used,
            "assigned": used,
            "reserved": reserved,
            "available": available,
            "total": total,
            "tracked": len(records),
            "percent": max(0, min(percent, 100)),
        }

    for block in blocks:
        block_id = str(block.id)
        pool = getattr(block, "pool", None)
        pool_id = str(getattr(block, "pool_id", "") or "")
        if (
            not pool_id
            or not pool
            or _ip_version_value(getattr(pool, "ip_version", None)) != "ipv4"
        ):
            block_utilization[block_id] = {
                "used": 0,
                "assigned": 0,
                "reserved": 0,
                "available": 0,
                "total": 0,
                "tracked": 0,
                "percent": 0,
            }
            continue
        network = _parse_network(str(block.cidr))
        if network is None or network.version != 4:
            block_utilization[block_id] = {
                "used": 0,
                "assigned": 0,
                "reserved": 0,
                "available": 0,
                "total": 0,
                "tracked": 0,
                "percent": 0,
            }
            continue
        allow_network_broadcast = _pool_allows_network_broadcast(pool)
        used = 0
        reserved = 0
        tracked = 0
        for record in ipv4_by_pool.get(pool_id, []):
            try:
                address = ipaddress.ip_address(str(record.address))
            except ValueError:
                continue
            if address not in network:
                continue
            tracked += 1
            if _active_assignment(record):
                used += 1
            elif getattr(record, "is_reserved", False):
                reserved += 1
        total = _ipv4_capacity_count(
            str(block.cidr), allow_network_broadcast=allow_network_broadcast
        )
        available = max(total - used - reserved, 0)
        percent = int(round((used / total) * 100)) if total > 0 else 0
        block_utilization[block_id] = {
            "used": used,
            "assigned": used,
            "reserved": reserved,
            "available": available,
            "total": total,
            "tracked": tracked,
            "percent": max(0, min(percent, 100)),
        }

    return pool_utilization, block_utilization


def _build_ipv4_range_rows(
    db,
    *,
    pool,
    cidr: str,
    limit: int = 256,
) -> dict[str, object] | None:
    metadata, _ = _pool_metadata(pool)
    allow_network_broadcast = (
        str(metadata.get("allow_network_broadcast") or "").lower() == "true"
    )
    network = _parse_network(cidr)
    if network is None or network.version != 4:
        return None

    address_records = {
        str(item.address): item
        for item in (
            db.query(IPv4Address)
            .options(joinedload(IPv4Address.assignment))
            .filter(IPv4Address.pool_id == pool.id)
            .all()
        )
    }
    all_hosts = (
        [str(ip) for ip in network]
        if allow_network_broadcast
        else [str(ip) for ip in network.hosts()]
    )
    limited_hosts = all_hosts[:limit] if limit > 0 else all_hosts

    rows: list[dict[str, object]] = []
    assigned_count = 0
    reserved_count = 0
    available_count = 0
    for ip in limited_hosts:
        record = address_records.get(ip)
        assignment = _active_assignment(record) if record else None
        subscriber = getattr(assignment, "subscriber", None) if assignment else None
        subscription = getattr(assignment, "subscription", None) if assignment else None
        status = "available"
        if record and getattr(record, "is_reserved", False) and not assignment:
            status = "reserved"
            reserved_count += 1
        elif assignment:
            status = "assigned"
            assigned_count += 1
        else:
            available_count += 1

        rows.append(
            {
                "ip_address": ip,
                "status": status,
                "search_text": (
                    f"{ip} "
                    f"{str(getattr(subscriber, 'full_name', '') or '').strip()} "
                    f"{str(getattr(subscriber, 'first_name', '') or '').strip()} "
                    f"{str(getattr(subscriber, 'last_name', '') or '').strip()} "
                    f"{str(getattr(subscriber, 'email', '') or '').strip()} "
                    f"{str(getattr(subscription, 'service_id', '') or '').strip()}"
                )
                .strip()
                .lower(),
                "subscriber_name": (
                    str(getattr(subscriber, "full_name", "") or "").strip()
                    or (
                        f"{str(getattr(subscriber, 'first_name', '') or '').strip()} "
                        f"{str(getattr(subscriber, 'last_name', '') or '').strip()}"
                    ).strip()
                    or str(getattr(subscriber, "email", "") or "").strip()
                    or None
                ),
                "subscriber_id": str(getattr(subscriber, "id", "") or "") or None,
                "subscription_id": str(getattr(subscription, "id", "") or "") or None,
                "service_ref": str(
                    getattr(subscription, "service_id", "") or ""
                ).strip()
                or None,
                "device": str(metadata.get("router") or "").strip() or None,
                "notes": str(getattr(record, "notes", "") or "").strip() or None,
            }
        )

    total_usable = _ipv4_capacity_count(
        str(cidr), allow_network_broadcast=allow_network_broadcast
    )
    if len(limited_hosts) < total_usable:
        available_count += total_usable - len(limited_hosts)

    return {
        "ip_rows": rows,
        "limit_applied": limit,
        "row_count": len(rows),
        "allow_network_broadcast": allow_network_broadcast,
        "usage_type": metadata.get("usage_type"),
        "router": metadata.get("router"),
        "stats": {
            "assigned": assigned_count,
            "reserved": reserved_count,
            "available": max(total_usable - assigned_count - reserved_count, 0),
            "total_usable": total_usable,
            "percent_used": int(round((assigned_count / total_usable) * 100))
            if total_usable > 0
            else 0,
        },
    }


def reconcile_ipv4_pool_memberships(db) -> dict[str, object]:
    pools = [
        pool
        for pool in network_service.ip_pools.list(
            db=db,
            ip_version="ipv4",
            is_active=None,
            order_by="name",
            order_dir="asc",
            limit=5000,
            offset=0,
        )
        if _parse_network(str(pool.cidr)) is not None
    ]
    pool_networks = [(pool, _parse_network(str(pool.cidr))) for pool in pools]

    addresses = db.query(IPv4Address).all()
    updated = 0
    unchanged = 0
    unmatched = 0
    invalid = 0
    conflicts = 0
    samples: list[dict[str, str]] = []

    for record in addresses:
        try:
            address = ipaddress.ip_address(str(record.address))
        except ValueError:
            invalid += 1
            continue
        matches = [
            pool for pool, network in pool_networks if network and address in network
        ]
        if not matches:
            unmatched += 1
            continue
        if len(matches) > 1:
            conflicts += 1
            continue
        target_pool = matches[0]
        current_pool_id = str(getattr(record, "pool_id", "") or "")
        if current_pool_id == str(target_pool.id):
            unchanged += 1
            continue
        record.pool_id = target_pool.id
        updated += 1
        if len(samples) < 25:
            samples.append(
                {
                    "address": str(record.address),
                    "pool": target_pool.name,
                    "cidr": target_pool.cidr,
                }
            )

    if updated:
        db.commit()

    return {
        "updated": updated,
        "unchanged": unchanged,
        "unmatched": unmatched,
        "invalid": invalid,
        "conflicts": conflicts,
        "sample_updates": samples,
        "pool_count": len(pools),
        "address_count": len(addresses),
    }


def _ipv4_address_in_scope(
    *,
    ip_address: str,
    pool,
    block=None,
) -> bool:
    pool_network = _parse_network(str(pool.cidr))
    if pool_network is None or pool_network.version != 4:
        return False
    try:
        parsed_ip = ipaddress.ip_address(ip_address)
    except ValueError:
        return False
    if parsed_ip not in pool_network:
        return False
    if (
        not _pool_allows_network_broadcast(pool)
        and parsed_ip not in pool_network.hosts()
    ):
        return False
    if block is not None:
        block_network = _parse_network(str(block.cidr))
        if (
            block_network is None
            or block_network.version != 4
            or parsed_ip not in block_network
        ):
            return False
        if (
            not _pool_allows_network_broadcast(pool)
            and parsed_ip not in block_network.hosts()
        ):
            return False
    return True


def build_ipv4_assignment_form_data(
    db,
    *,
    pool_id: str,
    ip_address: str,
    block_id: str | None = None,
) -> dict[str, object] | None:
    try:
        pool = network_service.ip_pools.get(db=db, pool_id=pool_id)
    except Exception:
        return None
    if _ip_version_value(getattr(pool, "ip_version", None)) != "ipv4":
        return None

    block = None
    if block_id:
        try:
            block = network_service.ip_blocks.get(db=db, block_id=block_id)
        except Exception:
            return None
        if str(getattr(block, "pool_id", "") or "") != str(pool.id):
            return None

    normalized_ip = str(ip_address or "").strip()
    if not _ipv4_address_in_scope(ip_address=normalized_ip, pool=pool, block=block):
        return None

    address_record = (
        db.query(IPv4Address)
        .options(
            joinedload(IPv4Address.assignment).joinedload(IPAssignment.subscriber),
            joinedload(IPv4Address.assignment).joinedload(IPAssignment.subscription),
        )
        .filter(IPv4Address.address == normalized_ip)
        .first()
    )
    active_assignment = _active_assignment(address_record) if address_record else None
    subscriber = (
        getattr(active_assignment, "subscriber", None) if active_assignment else None
    )
    subscription = (
        getattr(active_assignment, "subscription", None) if active_assignment else None
    )

    return {
        "pool": pool,
        "block": block,
        "ip_address": normalized_ip,
        "address_record": address_record,
        "assignment": active_assignment,
        "mode": "reassign" if active_assignment else "assign",
        "current_subscriber_label": _subscriber_display_name(subscriber),
        "current_subscription_label": _subscription_display_name(subscription),
        "subscriber_id": str(getattr(subscriber, "id", "") or "") or None,
        "subscription_id": str(getattr(subscription, "id", "") or "") or None,
    }


def assign_ipv4_address(
    db,
    *,
    pool_id: str,
    ip_address: str,
    subscriber_id: str,
    subscription_id: str | None = None,
    block_id: str | None = None,
) -> dict[str, object]:
    state = build_ipv4_assignment_form_data(
        db,
        pool_id=pool_id,
        ip_address=ip_address,
        block_id=block_id,
    )
    if state is None:
        raise ValueError("Selected IPv4 address is not valid for this range.")

    normalized_subscriber_id = str(subscriber_id or "").strip()
    if not normalized_subscriber_id:
        raise ValueError("Subscriber is required.")
    normalized_subscription_id = str(subscription_id or "").strip() or None

    pool = state["pool"]
    address_record = state["address_record"]
    active_assignment = state["assignment"]

    if address_record and getattr(address_record, "is_reserved", False):
        raise ValueError("Reserved IPv4 addresses cannot be assigned from this screen.")

    if address_record is None:
        address_record = IPv4Address(
            address=state["ip_address"],
            pool_id=pool.id,
            is_reserved=False,
        )
        db.add(address_record)
        db.commit()
        db.refresh(address_record)
    elif address_record.pool_id is None:
        address_record.pool_id = pool.id
        db.commit()
        db.refresh(address_record)
    elif str(address_record.pool_id) != str(pool.id):
        raise ValueError("IPv4 address belongs to a different pool.")

    existing_assignment = getattr(address_record, "assignment", None)
    previous_assignment = active_assignment or existing_assignment
    # Check if already assigned to the same subscriber - skip reassignment
    if active_assignment:
        if str(
            getattr(active_assignment, "subscriber_id", "") or ""
        ) == normalized_subscriber_id:
            return {
                "address": address_record,
                "assignment": active_assignment,
                "previous_assignment": previous_assignment,
                "created": False,
                "reassigned": False,
            }

    # IP assignments now link directly to subscribers, not subscriptions
    assignment_payload = {
        "account_id": UUID(normalized_subscriber_id),
        "ip_version": IPVersion.ipv4,
        "ipv4_address_id": address_record.id,
        "is_active": True,
    }
    if existing_assignment:
        assignment = network_service.ip_assignments.update(
            db=db,
            assignment_id=str(existing_assignment.id),
            payload=IPAssignmentUpdate.model_validate(assignment_payload),
        )
    else:
        assignment = network_service.ip_assignments.create(
            db=db,
            payload=IPAssignmentCreate.model_validate(assignment_payload),
        )

    if normalized_subscription_id:
        catalog_service.subscriptions.update(
            db=db,
            subscription_id=normalized_subscription_id,
            payload=SubscriptionUpdate.model_validate(
                {"ipv4_address": state["ip_address"]}
            ),
        )

    return {
        "address": address_record,
        "assignment": assignment,
        "previous_assignment": previous_assignment,
        "created": previous_assignment is None,
        "reassigned": previous_assignment is not None,
    }


def build_ip_management_data(
    db,
    *,
    page: int = 1,
    search: str | None = None,
    pool_filter: str | None = None,
    address_limit: int = 50,
) -> dict[str, object]:
    from sqlalchemy import func, select

    from app.models.network import IpBlock, IpPool, IPv4Address, IPv6Address

    total_pools = db.execute(select(func.count(IpPool.id))).scalar() or 0
    pools = network_service.ip_pools.list(
        db=db,
        ip_version=None,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=max(int(total_pools), 1),
        offset=0,
    )
    total_blocks = db.execute(select(func.count(IpBlock.id))).scalar() or 0
    blocks = network_service.ip_blocks.list(
        db=db,
        pool_id=None,
        is_active=None,
        order_by="cidr",
        order_dir="asc",
        limit=max(int(total_blocks), 1),
        offset=0,
    )
    assignments = network_service.ip_assignments.list(
        db=db,
        subscriber_id=None,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )
    pool_utilization, block_utilization = _build_pool_and_block_utilization(
        db,
        pools=pools,
        blocks=blocks,
    )

    # Pagination for addresses
    offset = (page - 1) * address_limit
    search_term = search.strip() if search else None
    pool_id = pool_filter if pool_filter else None

    # Get total counts using SQL
    total_ipv4 = (
        db.execute(
            select(func.count(IPv4Address.id)).where(IPv4Address.is_reserved == False)
        ).scalar()
        or 0
    )
    total_ipv6 = (
        db.execute(
            select(func.count(IPv6Address.id)).where(IPv6Address.is_reserved == False)
        ).scalar()
        or 0
    )

    # Fetch paginated addresses
    ipv4_addresses = network_service.ipv4_addresses.list(
        db=db,
        pool_id=pool_id,
        is_reserved=None,
        order_by="address",
        order_dir="asc",
        limit=address_limit,
        offset=offset,
    )
    ipv6_addresses = network_service.ipv6_addresses.list(
        db=db,
        pool_id=pool_id,
        is_reserved=None,
        order_by="address",
        order_dir="asc",
        limit=address_limit,
        offset=offset,
    )

    # If there's a search term, filter in-memory for better UX (server-side search)
    if search_term:
        ipv4_addresses = [
            addr for addr in ipv4_addresses if search_term in str(addr.address).lower()
        ]
        ipv6_addresses = [
            addr for addr in ipv6_addresses if search_term in str(addr.address).lower()
        ]

    total_addresses = total_ipv4 + total_ipv6
    total_pages = max(1, (total_addresses + address_limit - 1) // address_limit)

    stats = {
        "total_pools": len(pools),
        "total_blocks": len(blocks),
        "total_assignments": len(assignments),
        "total_addresses": total_addresses,
    }
    return {
        "pools": pools,
        "blocks": blocks,
        "pool_utilization": pool_utilization,
        "block_utilization": block_utilization,
        "assignments": assignments,
        "ipv4_addresses": ipv4_addresses,
        "ipv6_addresses": ipv6_addresses,
        "stats": stats,
        "pagination": {
            "page": page,
            "limit": address_limit,
            "total_pages": total_pages,
            "total_items": total_addresses,
        },
        "search": search_term,
        "pool_filter": pool_id,
    }


def get_ip_pool_new_form_data(db=None) -> dict[str, object]:
    return {
        "pool": None,
        "action_url": "/admin/network/ip-management/pools",
        "olt_devices": list_active_olts(db),
        "vlans": list_active_vlans(db),
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


def list_active_olts(db):
    if db is None:
        return []
    return network_service.olt_devices.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )


def list_active_vlans(db):
    if db is None:
        return []
    return network_service.vlans.list(
        db=db,
        region_id=None,
        is_active=True,
        order_by="tag",
        order_dir="asc",
        limit=1000,
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


def _ip_block_conflict_error(db, *, pool_id: str, cidr: str) -> str | None:
    pool = network_service.ip_pools.get(db=db, pool_id=pool_id)
    block_network = _parse_network(cidr)
    pool_network = _parse_network(pool.cidr)
    if block_network is None:
        return "CIDR block is invalid."
    if pool_network is None:
        return "Pool CIDR is invalid."
    if block_network.version != pool_network.version:
        return "Block IP version must match pool IP version."

    # Block must be fully inside the pool network
    if not block_network.subnet_of(pool_network):
        return f"Block CIDR {cidr} must be inside pool CIDR {pool.cidr}."

    existing_blocks = network_service.ip_blocks.list(
        db=db,
        pool_id=pool_id,
        is_active=None,
        order_by="cidr",
        order_dir="asc",
        limit=5000,
        offset=0,
    )
    for existing in existing_blocks:
        existing_network = _parse_network(existing.cidr)
        if existing_network is None:
            continue
        if block_network.overlaps(existing_network):
            return f"Block CIDR {cidr} overlaps existing block {existing.cidr}."
    return None


def create_ip_block(db, values: dict[str, object]):
    try:
        normalized = dict(values)
        normalized["pool_id"] = coerce_uuid(str(values.get("pool_id") or ""))
        payload = IpBlockCreate.model_validate(normalized)
        conflict_error = _ip_block_conflict_error(
            db,
            pool_id=str(payload.pool_id),
            cidr=str(payload.cidr),
        )
        if conflict_error:
            return None, conflict_error
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
        "olt_device_id": form.get("olt_device_id", "").strip() or None,
        "vlan_id": form.get("vlan_id", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
        "location": form.get("location", "").strip() or None,
        "category": form.get("category", "").strip() or None,
        "network_type": form.get("network_type", "").strip() or None,
        "router": form.get("router", "").strip() or None,
        "usage_type": form.get("usage_type", "").strip() or None,
        "allow_network_broadcast": form.get("allow_network_broadcast") == "true",
        "is_fallback": form.get("is_fallback") == "true",
        "is_active": form.get("is_active") == "true",
    }


def parse_ipv6_network_form(form) -> dict[str, object]:
    network = str(form.get("network") or "").strip()
    prefix = str(form.get("prefix_length") or "").strip() or "64"
    cidr = f"{network}/{prefix}" if network else ""
    return {
        "name": str(form.get("title") or "").strip(),
        "ip_version": "ipv6",
        "cidr": cidr,
        "gateway": str(form.get("gateway") or "").strip() or None,
        "dns_primary": str(form.get("dns_primary") or "").strip() or None,
        "dns_secondary": str(form.get("dns_secondary") or "").strip() or None,
        "olt_device_id": str(form.get("olt_device_id") or "").strip() or None,
        "vlan_id": str(form.get("vlan_id") or "").strip() or None,
        "notes": str(form.get("comment") or "").strip() or None,
        "location": str(form.get("location") or "").strip() or None,
        "category": str(form.get("category") or "").strip() or None,
        "network_type": str(form.get("network_type") or "").strip() or None,
        "router": str(form.get("router") or "").strip() or None,
        "usage_type": str(form.get("usage_type") or "").strip() or None,
        "allow_network_broadcast": False,
        "is_fallback": False,
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


def _ip_pool_scope_error(db, values: dict[str, object]) -> str | None:
    vlan_id = str(values.get("vlan_id") or "").strip()
    olt_id = str(values.get("olt_device_id") or "").strip()
    if not vlan_id:
        return None
    try:
        vlan = network_service.vlans.get(db=db, vlan_id=vlan_id)
    except Exception:
        return "Selected VLAN was not found."
    vlan_olt_id = str(getattr(vlan, "olt_device_id", "") or "")
    if olt_id and vlan_olt_id and vlan_olt_id != olt_id:
        return "Selected VLAN belongs to a different OLT."
    if not olt_id and vlan_olt_id:
        values["olt_device_id"] = vlan_olt_id
    return None


def pool_form_snapshot(
    values: dict[str, object], *, pool_id: str | None = None
) -> dict[str, object]:
    data = {
        "name": values.get("name"),
        "ip_version": {"value": values.get("ip_version")},
        "cidr": values.get("cidr"),
        "gateway": values.get("gateway"),
        "dns_primary": values.get("dns_primary"),
        "dns_secondary": values.get("dns_secondary"),
        "olt_device_id": values.get("olt_device_id"),
        "vlan_id": values.get("vlan_id"),
        "notes": values.get("notes"),
        "location": values.get("location"),
        "category": values.get("category"),
        "network_type": values.get("network_type"),
        "router": values.get("router"),
        "usage_type": values.get("usage_type"),
        "allow_network_broadcast": values.get("allow_network_broadcast"),
        "is_fallback": values.get("is_fallback"),
        "is_active": values.get("is_active"),
    }
    if pool_id:
        data["id"] = pool_id
    return data


def pool_form_snapshot_from_model(pool) -> dict[str, object]:
    notes_without_fallback = _strip_fallback_marker(getattr(pool, "notes", None))
    metadata, cleaned_notes = parse_pool_notes_metadata(notes_without_fallback)
    return {
        "id": str(pool.id),
        "name": pool.name,
        "ip_version": {"value": pool.ip_version.value},
        "cidr": pool.cidr,
        "gateway": pool.gateway,
        "dns_primary": pool.dns_primary,
        "dns_secondary": pool.dns_secondary,
        "olt_device_id": str(pool.olt_device_id) if pool.olt_device_id else None,
        "vlan_id": str(getattr(pool, "vlan_id", None) or "") or None,
        "notes": cleaned_notes,
        "location": metadata.get("location"),
        "category": metadata.get("category"),
        "network_type": metadata.get("network_type"),
        "router": metadata.get("router"),
        "usage_type": metadata.get("usage_type"),
        "allow_network_broadcast": str(
            metadata.get("allow_network_broadcast") or ""
        ).lower()
        == "true",
        "is_fallback": is_fallback_pool_notes(pool.notes),
        "is_active": pool.is_active,
    }


def create_ip_pool(db, values: dict[str, object]):
    try:
        from app.models.network import IPVersion

        normalized = dict(values)
        normalized["notes"] = normalize_pool_notes(
            notes=str(values.get("notes") or "").strip() or None,
            is_fallback=bool(values.get("is_fallback")),
            location=str(values.get("location") or "").strip() or None,
            category=str(values.get("category") or "").strip() or None,
            network_type=str(values.get("network_type") or "").strip() or None,
            router=str(values.get("router") or "").strip() or None,
            usage_type=str(values.get("usage_type") or "").strip() or None,
            allow_network_broadcast=(
                bool(values.get("allow_network_broadcast"))
                if values.get("allow_network_broadcast") is not None
                else None
            ),
        )
        normalized.pop("is_fallback", None)
        normalized.pop("location", None)
        normalized.pop("category", None)
        normalized.pop("network_type", None)
        normalized.pop("router", None)
        scope_error = _ip_pool_scope_error(db, normalized)
        if scope_error:
            return None, scope_error
        normalized["ip_version"] = validate_enum(
            str(values.get("ip_version") or ""), IPVersion, "ip_version"
        )
        payload = IpPoolCreate.model_validate(normalized)
        overlap_error = _overlapping_pool_error(
            db,
            cidr=str(payload.cidr),
            ip_version_value=payload.ip_version.value,
        )
        if overlap_error:
            return None, overlap_error
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
        normalized["notes"] = normalize_pool_notes(
            notes=str(values.get("notes") or "").strip() or None,
            is_fallback=bool(values.get("is_fallback")),
            location=str(values.get("location") or "").strip() or None,
            category=str(values.get("category") or "").strip() or None,
            network_type=str(values.get("network_type") or "").strip() or None,
            router=str(values.get("router") or "").strip() or None,
            usage_type=str(values.get("usage_type") or "").strip() or None,
            allow_network_broadcast=(
                bool(values.get("allow_network_broadcast"))
                if values.get("allow_network_broadcast") is not None
                else None
            ),
        )
        normalized.pop("is_fallback", None)
        normalized.pop("location", None)
        normalized.pop("category", None)
        normalized.pop("network_type", None)
        normalized.pop("router", None)
        scope_error = _ip_pool_scope_error(db, normalized)
        if scope_error:
            return None, None, scope_error
        if values.get("ip_version"):
            normalized["ip_version"] = validate_enum(
                str(values.get("ip_version") or ""), IPVersion, "ip_version"
            )
        payload = IpPoolUpdate.model_validate(normalized)
        effective_cidr = str(payload.cidr or current.cidr)
        effective_version = (
            payload.ip_version.value
            if payload.ip_version is not None
            else current.ip_version.value
        )
        overlap_error = _overlapping_pool_error(
            db,
            cidr=effective_cidr,
            ip_version_value=effective_version,
            exclude_pool_id=str(current.id),
        )
        if overlap_error:
            return None, None, overlap_error
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
            db.query(IPv4Address)
            .options(joinedload(IPv4Address.assignment))
            .filter(IPv4Address.pool_id == pool.id)
            .limit(100)
            .all()
        )
    else:
        assignments = (
            db.query(IPv6Address)
            .options(joinedload(IPv6Address.assignment))
            .filter(IPv6Address.pool_id == pool.id)
            .limit(100)
            .all()
        )
    pool_utilization, _ = _build_pool_and_block_utilization(
        db, pools=[pool], blocks=blocks
    )
    return {
        "pool": pool,
        "blocks": blocks,
        "assignments": assignments,
        "utilization": pool_utilization.get(
            str(pool.id),
            {"used": 0, "reserved": 0, "available": 0, "total": 0, "percent": 0},
        ),
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


def build_dual_stack_data(
    db,
    *,
    view_mode: str = "subscriber",
    subscriber_query: str | None = None,
    location_query: str | None = None,
) -> dict[str, object]:
    assignments = network_service.ip_assignments.list(
        db=db,
        subscriber_id=None,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )

    normalized_view_mode = str(view_mode or "subscriber").strip().lower()
    if normalized_view_mode not in {"subscriber", "location"}:
        normalized_view_mode = "subscriber"

    subscriber_query_text = str(subscriber_query or "").strip()
    location_query_text = str(location_query or "").strip()
    subscriber_filter = subscriber_query_text.lower()
    location_filter = location_query_text.lower()

    # Group by subscriber + service_address (devices link to subscribers, not subscriptions)
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for assignment in assignments:
        subscriber_id = str(getattr(assignment, "subscriber_id", "") or "")
        service_address_id = str(getattr(assignment, "service_address_id", "") or "")
        key = (subscriber_id, service_address_id)

        row = grouped.get(key)
        if row is None:
            subscriber = getattr(assignment, "subscriber", None)
            address = getattr(assignment, "service_address", None)
            location_parts = [
                str(getattr(address, "city", "") or "").strip(),
                str(getattr(address, "region", "") or "").strip(),
            ]
            location = (
                ", ".join(part for part in location_parts if part)
                or str(getattr(address, "address_line1", "") or "").strip()
            )
            display_name = (
                str(getattr(subscriber, "full_name", "") or "").strip()
                or (
                    f"{str(getattr(subscriber, 'first_name', '') or '').strip()} "
                    f"{str(getattr(subscriber, 'last_name', '') or '').strip()}"
                ).strip()
            )
            row = {
                "subscriber_id": subscriber_id or None,
                "subscriber_name": display_name or "Unknown Subscriber",
                "account_number": str(
                    getattr(subscriber, "account_number", "") or ""
                ).strip()
                or None,
                "service_address_id": service_address_id or None,
                "location": location or None,
                "ipv4_address": None,
                "ipv6_address": None,
                "is_dual_stack": False,
                "created_at": getattr(assignment, "created_at", None),
            }
            grouped[key] = row

        if getattr(assignment, "ipv4_address", None) is not None:
            row["ipv4_address"] = (
                str(getattr(assignment.ipv4_address, "address", "") or "").strip()
                or None
            )
        if getattr(assignment, "ipv6_address", None) is not None:
            address = str(getattr(assignment.ipv6_address, "address", "") or "").strip()
            prefix = getattr(assignment, "prefix_length", None)
            row["ipv6_address"] = f"{address}/{prefix}" if prefix else (address or None)

    rows = list(grouped.values())
    for row in rows:
        row["is_dual_stack"] = bool(row.get("ipv4_address")) and bool(
            row.get("ipv6_address")
        )

    if subscriber_filter:
        rows = [
            row
            for row in rows
            if subscriber_filter in str(row.get("subscriber_name") or "").lower()
            or subscriber_filter in str(row.get("account_number") or "").lower()
        ]
    if location_filter:
        rows = [
            row
            for row in rows
            if location_filter in str(row.get("location") or "").lower()
        ]

    if normalized_view_mode == "location":
        rows.sort(
            key=lambda row: (
                str(row.get("location") or "").lower(),
                str(row.get("subscriber_name") or "").lower(),
            )
        )
    else:
        rows.sort(
            key=lambda row: (
                str(row.get("subscriber_name") or "").lower(),
                str(row.get("location") or "").lower(),
            )
        )

    stats = {
        "total_records": len(rows),
        "dual_stack_records": sum(1 for row in rows if row.get("is_dual_stack")),
        "ipv4_only": sum(
            1 for row in rows if row.get("ipv4_address") and not row.get("ipv6_address")
        ),
        "ipv6_only": sum(
            1 for row in rows if row.get("ipv6_address") and not row.get("ipv4_address")
        ),
    }
    return {
        "rows": rows,
        "view_mode": normalized_view_mode,
        "subscriber_query": subscriber_query_text,
        "location_query": location_query_text,
        "stats": stats,
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
        "reserved": sum(
            1 for a in addresses if a.is_reserved and not _active_assignment(a)
        ),
        "assigned": sum(1 for a in addresses if _active_assignment(a)),
        "available": sum(
            1 for a in addresses if not a.is_reserved and not _active_assignment(a)
        ),
    }
    return {
        "addresses": addresses,
        "pools": pools,
        "stats": stats,
        "ip_version": ip_version,
    }


def build_ip_pools_data(db, *, pool_type: str = "all") -> dict[str, object]:
    from app.models.network import IpBlock, IpPool

    total_pools = _query_count(db, IpPool)
    pools_all = network_service.ip_pools.list(
        db=db,
        ip_version=None,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=max(int(total_pools), 1),
        offset=0,
    )
    normalized_pool_type = str(pool_type or "all").strip().lower()
    if normalized_pool_type not in {"all", "fallback", "standard"}:
        normalized_pool_type = "all"

    fallback_pool_ids = {
        str(pool.id)
        for pool in pools_all
        if is_fallback_pool_notes(getattr(pool, "notes", None))
    }
    if normalized_pool_type == "fallback":
        pools = [pool for pool in pools_all if str(pool.id) in fallback_pool_ids]
    elif normalized_pool_type == "standard":
        pools = [pool for pool in pools_all if str(pool.id) not in fallback_pool_ids]
    else:
        pools = pools_all

    selected_pool_ids = {str(pool.id) for pool in pools}
    total_blocks = _query_count(db, IpBlock, IpBlock.is_active.is_(True))
    blocks = network_service.ip_blocks.list(
        db=db,
        pool_id=None,
        is_active=True,
        order_by="cidr",
        order_dir="asc",
        limit=max(int(total_blocks), 1),
        offset=0,
    )
    if normalized_pool_type in {"fallback", "standard"}:
        blocks = [
            block
            for block in blocks
            if str(getattr(block, "pool_id", "")) in selected_pool_ids
        ]
    pool_utilization, block_utilization = _build_pool_and_block_utilization(
        db,
        pools=pools,
        blocks=blocks,
    )

    stats = {
        "total_pools": len(pools),
        "total_blocks": len(blocks),
        "ipv4_pools": sum(1 for p in pools if p.ip_version.value == "ipv4"),
        "ipv6_pools": sum(1 for p in pools if p.ip_version.value == "ipv6"),
        "fallback_pools": sum(1 for p in pools if str(p.id) in fallback_pool_ids),
    }
    return {
        "pools": pools,
        "blocks": blocks,
        "pool_utilization": pool_utilization,
        "block_utilization": block_utilization,
        "pool_type": normalized_pool_type,
        "fallback_pool_ids": fallback_pool_ids,
        "stats": stats,
    }


def build_ipv6_networks_data(
    db,
    *,
    location: str | None = None,
    category: str | None = None,
    sort_by: str = "cidr",
    sort_dir: str = "asc",
) -> dict[str, object]:
    state = build_ip_pools_data(db, pool_type="all")
    pools = [pool for pool in state["pools"] if pool.ip_version.value == "ipv6"]
    pool_utilization = state["pool_utilization"]

    networks: list[dict[str, object]] = []
    for pool in pools:
        metadata, cleaned_notes = parse_pool_notes_metadata(
            getattr(pool, "notes", None)
        )
        prefix_length = None
        try:
            prefix_length = ipaddress.ip_network(str(pool.cidr), strict=False).prefixlen
        except ValueError:
            prefix_length = None
        networks.append(
            {
                "pool": pool,
                "prefix_length": prefix_length,
                "utilization": pool_utilization.get(
                    str(pool.id), {"used": 0, "total": 0, "percent": 0}
                ),
                "location": metadata.get("location"),
                "category": metadata.get("category"),
                "network_type": metadata.get("network_type"),
                "router": metadata.get("router"),
                "notes": cleaned_notes,
            }
        )

    location_filter = str(location or "").strip().lower()
    category_filter = str(category or "").strip().lower()
    if location_filter:
        networks = [
            item
            for item in networks
            if str(item.get("location") or "").strip().lower() == location_filter
        ]
    if category_filter:
        networks = [
            item
            for item in networks
            if str(item.get("category") or "").strip().lower() == category_filter
        ]

    sort_key = str(sort_by or "cidr").strip().lower()
    reverse = str(sort_dir or "asc").strip().lower() == "desc"
    allowed_sort = {
        "id",
        "cidr",
        "prefix",
        "title",
        "location",
        "category",
        "network_type",
        "router",
        "utilization",
    }
    if sort_key not in allowed_sort:
        sort_key = "cidr"

    def _sort_value(item: dict[str, object]):
        pool = item["pool"]
        if sort_key == "id":
            return str(pool.id)
        if sort_key == "cidr":
            return str(pool.cidr or "")
        if sort_key == "prefix":
            return int(item.get("prefix_length") or 0)
        if sort_key == "title":
            return str(pool.name or "").lower()
        if sort_key == "location":
            return str(item.get("location") or "").lower()
        if sort_key == "category":
            return str(item.get("category") or "").lower()
        if sort_key == "network_type":
            return str(item.get("network_type") or "").lower()
        if sort_key == "router":
            return str(item.get("router") or "").lower()
        if sort_key == "utilization":
            util = item.get("utilization") or {}
            return int(util.get("percent") or 0)
        return str(pool.cidr or "")

    networks.sort(key=_sort_value, reverse=reverse)

    locations = sorted(
        {str(item["location"]) for item in networks if item.get("location")}
    )
    categories = sorted(
        {str(item["category"]) for item in networks if item.get("category")}
    )

    return {
        "networks": networks,
        "locations": locations,
        "categories": categories,
        "active_location": location_filter,
        "active_category": category_filter,
        "sort_by": sort_key,
        "sort_dir": "desc" if reverse else "asc",
        "stats": {
            "total_networks": len(networks),
            "total_used": sum(
                int((item.get("utilization") or {}).get("used") or 0)
                for item in networks
            ),
            "total_capacity": sum(
                int((item.get("utilization") or {}).get("total") or 0)
                for item in networks
            ),
        },
    }


def _ipv4_subnet_mask(prefix_length: int) -> str:
    mask = (0xFFFFFFFF << (32 - prefix_length)) & 0xFFFFFFFF if prefix_length > 0 else 0
    return ".".join(str((mask >> (8 * shift)) & 0xFF) for shift in (3, 2, 1, 0))


def build_ipv4_networks_data(
    db,
    *,
    location: str | None = None,
    category: str | None = None,
    network_type: str | None = None,
    sort_by: str = "cidr",
    sort_dir: str = "asc",
) -> dict[str, object]:
    state = build_ip_pools_data(db, pool_type="all")
    pools = [pool for pool in state["pools"] if pool.ip_version.value == "ipv4"]
    pool_utilization = state["pool_utilization"]

    networks: list[dict[str, object]] = []
    for pool in pools:
        metadata, cleaned_notes = parse_pool_notes_metadata(
            getattr(pool, "notes", None)
        )
        try:
            cidr = ipaddress.ip_network(str(pool.cidr), strict=False)
        except ValueError:
            continue
        prefix_length = cidr.prefixlen
        subnet_mask = _ipv4_subnet_mask(prefix_length)
        total_ips = int(cidr.num_addresses)
        usable_hosts = _usable_ipv4_count(str(pool.cidr))
        networks.append(
            {
                "pool": pool,
                "network": str(cidr.network_address),
                "prefix_length": prefix_length,
                "subnet_mask": subnet_mask,
                "total_ips": total_ips,
                "usable_hosts": usable_hosts,
                "utilization": pool_utilization.get(
                    str(pool.id), {"used": 0, "total": 0, "percent": 0}
                ),
                "location": metadata.get("location"),
                "category": metadata.get("category"),
                "network_type": metadata.get("network_type"),
                "router": metadata.get("router"),
                "usage_type": metadata.get("usage_type"),
                "allow_network_broadcast": str(
                    metadata.get("allow_network_broadcast") or ""
                ).lower()
                == "true",
                "notes": cleaned_notes,
            }
        )

    location_filter = str(location or "").strip().lower()
    category_filter = str(category or "").strip().lower()
    network_type_filter = str(network_type or "").strip().lower()
    if location_filter:
        networks = [
            item
            for item in networks
            if str(item.get("location") or "").strip().lower() == location_filter
        ]
    if category_filter:
        networks = [
            item
            for item in networks
            if str(item.get("category") or "").strip().lower() == category_filter
        ]
    if network_type_filter:
        networks = [
            item
            for item in networks
            if str(item.get("network_type") or "").strip().lower()
            == network_type_filter
        ]

    sort_key = str(sort_by or "cidr").strip().lower()
    reverse = str(sort_dir or "asc").strip().lower() == "desc"
    allowed_sort = {
        "id",
        "network",
        "cidr",
        "prefix",
        "title",
        "location",
        "category",
        "network_type",
        "router",
        "utilization",
    }
    if sort_key not in allowed_sort:
        sort_key = "cidr"

    def _sort_value(item: dict[str, object]):
        pool = item["pool"]
        if sort_key == "id":
            return str(pool.id)
        if sort_key == "network":
            return str(item.get("network") or "")
        if sort_key == "cidr":
            return str(pool.cidr or "")
        if sort_key == "prefix":
            return int(item.get("prefix_length") or 0)
        if sort_key == "title":
            return str(pool.name or "").lower()
        if sort_key == "location":
            return str(item.get("location") or "").lower()
        if sort_key == "category":
            return str(item.get("category") or "").lower()
        if sort_key == "network_type":
            return str(item.get("network_type") or "").lower()
        if sort_key == "router":
            return str(item.get("router") or "").lower()
        if sort_key == "utilization":
            util = item.get("utilization") or {}
            return int(util.get("percent") or 0)
        return str(pool.cidr or "")

    networks.sort(key=_sort_value, reverse=reverse)

    locations = sorted(
        {str(item["location"]) for item in networks if item.get("location")}
    )
    categories = sorted(
        {str(item["category"]) for item in networks if item.get("category")}
    )
    network_types = sorted(
        {str(item["network_type"]) for item in networks if item.get("network_type")}
    )

    return {
        "networks": networks,
        "locations": locations,
        "categories": categories,
        "network_types": network_types,
        "active_location": location_filter,
        "active_category": category_filter,
        "active_network_type": network_type_filter,
        "sort_by": sort_key,
        "sort_dir": "desc" if reverse else "asc",
        "stats": {
            "total_networks": len(networks),
            "total_used": sum(
                int((item.get("utilization") or {}).get("used") or 0)
                for item in networks
            ),
            "total_capacity": sum(
                int((item.get("utilization") or {}).get("total") or 0)
                for item in networks
            ),
        },
    }


def build_ipv4_network_detail_data(
    db,
    *,
    pool_id: str,
    limit: int = 256,
) -> dict[str, object] | None:
    base = build_ip_pool_detail_data(db, pool_id=pool_id)
    if base is None:
        return None
    pool = base["pool"]
    if getattr(pool, "ip_version", None) is None or pool.ip_version.value != "ipv4":
        return None

    rows_state = _build_ipv4_range_rows(
        db,
        pool=pool,
        cidr=str(pool.cidr),
        limit=limit,
    )
    if rows_state is None:
        return None
    base.update(rows_state)
    base["detail_scope"] = "pool"
    return base


def build_ipv4_block_detail_data(
    db,
    *,
    block_id: str,
    limit: int = 256,
) -> dict[str, object] | None:
    try:
        block = network_service.ip_blocks.get(db=db, block_id=block_id)
    except Exception:
        return None

    pool = getattr(block, "pool", None)
    if pool is None or _ip_version_value(getattr(pool, "ip_version", None)) != "ipv4":
        return None

    rows_state = _build_ipv4_range_rows(
        db,
        pool=pool,
        cidr=str(block.cidr),
        limit=limit,
    )
    if rows_state is None:
        return None

    return {
        "pool": pool,
        "block": block,
        "detail_scope": "block",
        **rows_state,
    }
