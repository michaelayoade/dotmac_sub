"""Service helpers for admin network IP-management web routes."""

from __future__ import annotations

import csv
import ipaddress
import io
import re
from collections import defaultdict

from pydantic import ValidationError

from app.models.network import IPv4Address, IPv6Address
from app.schemas.network import IpBlockCreate, IpPoolCreate, IpPoolUpdate
from app.services import network as network_service
from app.services.audit_helpers import diff_dicts, model_to_dict
from app.services.common import coerce_uuid, validate_enum

_FALLBACK_MARKER = "[fallback]"
_POOL_META_KEYS = ("location", "category", "network_type", "router")


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


def _parse_network(cidr: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None


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
            return (
                f"CIDR {cidr} overlaps existing pool {pool.name} ({pool.cidr})."
            )
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


def parse_pool_notes_metadata(notes: str | None) -> tuple[dict[str, str | None], str | None]:
    text = str(notes or "").strip()
    metadata: dict[str, str | None] = {key: None for key in _POOL_META_KEYS}
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
                value = stripped[len(prefix):-1].strip()
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
        row: dict[str, str] = {str(k or "").strip().lower(): str(v or "").strip() for k, v in raw.items()}
        normalized_rows.append(row)
    return normalized_rows


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
        cidr = row.get("cidr", "")
        name = row.get("name", "") or f"Imported {cidr or index}"
        payload = {
            "name": name,
            "ip_version": row.get("ip_version", "") or default_ip_version,
            "cidr": cidr,
            "gateway": row.get("gateway") or None,
            "dns_primary": row.get("dns_primary") or None,
            "dns_secondary": row.get("dns_secondary") or None,
            "notes": normalize_pool_notes(
                notes=row.get("notes") or None,
                is_fallback=_normalize_bool(row.get("is_fallback"), False),
                location=row.get("location") or None,
                category=row.get("category") or None,
                network_type=row.get("network_type") or None,
                router=row.get("router") or None,
            ),
            "is_active": _normalize_bool(row.get("is_active"), True),
        }
        validation_error = validate_ip_pool_values(payload)
        if validation_error:
            errors.append({"line": index, "name": name, "cidr": cidr, "error": validation_error})
            continue
        pool, error = create_ip_pool(db, payload)
        if error or pool is None:
            errors.append({"line": index, "name": name, "cidr": cidr, "error": error or "Unknown error"})
            continue
        created.append(pool)

    return {"created": created, "errors": errors, "total_rows": len(rows)}


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
        "notes": form.get("notes", "").strip() or None,
        "location": form.get("location", "").strip() or None,
        "category": form.get("category", "").strip() or None,
        "network_type": form.get("network_type", "").strip() or None,
        "router": form.get("router", "").strip() or None,
        "is_fallback": form.get("is_fallback") == "true",
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
        "location": values.get("location"),
        "category": values.get("category"),
        "network_type": values.get("network_type"),
        "router": values.get("router"),
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
        "notes": cleaned_notes,
        "location": metadata.get("location"),
        "category": metadata.get("category"),
        "network_type": metadata.get("network_type"),
        "router": metadata.get("router"),
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
        )
        normalized.pop("is_fallback", None)
        normalized.pop("location", None)
        normalized.pop("category", None)
        normalized.pop("network_type", None)
        normalized.pop("router", None)
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
        )
        normalized.pop("is_fallback", None)
        normalized.pop("location", None)
        normalized.pop("category", None)
        normalized.pop("network_type", None)
        normalized.pop("router", None)
        if values.get("ip_version"):
            normalized["ip_version"] = validate_enum(
                str(values.get("ip_version") or ""), IPVersion, "ip_version"
            )
        payload = IpPoolUpdate.model_validate(normalized)
        effective_cidr = str(payload.cidr or current.cidr)
        effective_version = (
            payload.ip_version.value if payload.ip_version is not None else current.ip_version.value
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
        subscription_id=None,
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

    grouped: dict[tuple[str, str, str], dict[str, object]] = {}
    for assignment in assignments:
        subscriber_id = str(getattr(assignment, "subscriber_id", "") or "")
        subscription_id = str(getattr(assignment, "subscription_id", "") or "")
        service_address_id = str(getattr(assignment, "service_address_id", "") or "")
        key = (subscriber_id, subscription_id, service_address_id)

        row = grouped.get(key)
        if row is None:
            subscriber = getattr(assignment, "subscriber", None)
            address = getattr(assignment, "service_address", None)
            location_parts = [
                str(getattr(address, "city", "") or "").strip(),
                str(getattr(address, "region", "") or "").strip(),
            ]
            location = ", ".join(part for part in location_parts if part) or str(
                getattr(address, "address_line1", "") or ""
            ).strip()
            display_name = str(getattr(subscriber, "full_name", "") or "").strip() or (
                f"{str(getattr(subscriber, 'first_name', '') or '').strip()} "
                f"{str(getattr(subscriber, 'last_name', '') or '').strip()}"
            ).strip()
            row = {
                "subscriber_id": subscriber_id or None,
                "subscriber_name": display_name or "Unknown Subscriber",
                "account_number": str(getattr(subscriber, "account_number", "") or "").strip() or None,
                "subscription_id": subscription_id or None,
                "subscription_ref": str(
                    getattr(getattr(assignment, "subscription", None), "service_id", "") or ""
                ).strip() or None,
                "service_address_id": service_address_id or None,
                "location": location or None,
                "ipv4_address": None,
                "ipv6_address": None,
                "is_dual_stack": False,
                "created_at": getattr(assignment, "created_at", None),
            }
            grouped[key] = row

        if getattr(assignment, "ipv4_address", None) is not None:
            row["ipv4_address"] = str(getattr(assignment.ipv4_address, "address", "") or "").strip() or None
        if getattr(assignment, "ipv6_address", None) is not None:
            address = str(getattr(assignment.ipv6_address, "address", "") or "").strip()
            prefix = getattr(assignment, "prefix_length", None)
            row["ipv6_address"] = f"{address}/{prefix}" if prefix else (address or None)

    rows = list(grouped.values())
    for row in rows:
        row["is_dual_stack"] = bool(row.get("ipv4_address")) and bool(row.get("ipv6_address"))

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
        "ipv4_only": sum(1 for row in rows if row.get("ipv4_address") and not row.get("ipv6_address")),
        "ipv6_only": sum(1 for row in rows if row.get("ipv6_address") and not row.get("ipv4_address")),
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
        "reserved": sum(1 for a in addresses if a.is_reserved),
        "available": sum(1 for a in addresses if not a.is_reserved),
    }
    return {
        "addresses": addresses,
        "pools": pools,
        "stats": stats,
        "ip_version": ip_version,
    }


def build_ip_pools_data(db, *, pool_type: str = "all") -> dict[str, object]:
    pools_all = network_service.ip_pools.list(
        db=db,
        ip_version=None,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    normalized_pool_type = str(pool_type or "all").strip().lower()
    if normalized_pool_type not in {"all", "fallback", "standard"}:
        normalized_pool_type = "all"

    fallback_pool_ids = {
        str(pool.id) for pool in pools_all if is_fallback_pool_notes(getattr(pool, "notes", None))
    }
    if normalized_pool_type == "fallback":
        pools = [pool for pool in pools_all if str(pool.id) in fallback_pool_ids]
    elif normalized_pool_type == "standard":
        pools = [pool for pool in pools_all if str(pool.id) not in fallback_pool_ids]
    else:
        pools = pools_all

    selected_pool_ids = {str(pool.id) for pool in pools}
    blocks = network_service.ip_blocks.list(
        db=db,
        pool_id=None,
        is_active=True,
        order_by="cidr",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    if normalized_pool_type in {"fallback", "standard"}:
        blocks = [block for block in blocks if str(getattr(block, "pool_id", "")) in selected_pool_ids]
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
    ipv6_records = network_service.ipv6_addresses.list(
        db=db,
        pool_id=None,
        is_reserved=None,
        order_by="address",
        order_dir="asc",
        limit=50000,
        offset=0,
    )
    ipv6_by_pool: dict[str, list[str]] = defaultdict(list)
    for record in ipv6_records:
        if record.pool_id and str(record.pool_id) in selected_pool_ids:
            ipv6_by_pool[str(record.pool_id)].append(str(record.address))

    pool_utilization: dict[str, dict[str, int]] = {}
    for pool in pools:
        pool_id = str(pool.id)
        if pool.ip_version.value == "ipv4":
            total = _usable_ipv4_count(str(pool.cidr))
            used = len(ipv4_by_pool.get(pool_id, []))
        else:
            total = _ipv6_capacity_count(str(pool.cidr))
            used = len(ipv6_by_pool.get(pool_id, []))
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
        metadata, cleaned_notes = parse_pool_notes_metadata(getattr(pool, "notes", None))
        prefix_length = None
        try:
            prefix_length = ipaddress.ip_network(str(pool.cidr), strict=False).prefixlen
        except ValueError:
            prefix_length = None
        networks.append(
            {
                "pool": pool,
                "prefix_length": prefix_length,
                "utilization": pool_utilization.get(str(pool.id), {"used": 0, "total": 0, "percent": 0}),
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
            item for item in networks if str(item.get("location") or "").strip().lower() == location_filter
        ]
    if category_filter:
        networks = [
            item for item in networks if str(item.get("category") or "").strip().lower() == category_filter
        ]

    sort_key = str(sort_by or "cidr").strip().lower()
    reverse = str(sort_dir or "asc").strip().lower() == "desc"
    allowed_sort = {"id", "cidr", "prefix", "title", "location", "category", "network_type", "router", "utilization"}
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

    locations = sorted({str(item["location"]) for item in networks if item.get("location")})
    categories = sorted({str(item["category"]) for item in networks if item.get("category")})

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
            "total_used": sum(int((item.get("utilization") or {}).get("used") or 0) for item in networks),
            "total_capacity": sum(int((item.get("utilization") or {}).get("total") or 0) for item in networks),
        },
    }
