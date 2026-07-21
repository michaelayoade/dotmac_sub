"""IPAM ledger page data — a projection over the IP-management models.

Consolidates the pool / block / delegated-prefix inventory into one archetype-D
ledger with a facet per record type:
  - pools: IP pools with live utilisation (used/total from the snapshot owner)
  - blocks: IP blocks, with their owning pool
  - ipv6_prefixes: IPv6 delegated prefixes, state via the server-owned tone

IPAM has no CRUD-manager list owner — the address models are the source and the
admin report reads them directly (via ip_pool_utilization_snapshot), so this is
a read-only projection of those same rows, not a parallel authority. Live pool
utilisation comes from the snapshot owner's live_pool_counts.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.network import (
    IpBlock,
    IpPool,
    Ipv6DelegatedPrefix,
)
from app.services.ip_pool_utilization_snapshot import live_pool_counts
from app.services.status_presentation import ipv6_prefix_state_presentation

_LIMIT = 500

# facet order + labels
FACETS: tuple[tuple[str, str], ...] = (
    ("pools", "IP pools"),
    ("blocks", "IP blocks"),
    ("ipv6_prefixes", "IPv6 delegated prefixes"),
)
_VALID = {key for key, _ in FACETS}


def _pool_rows(db: Session) -> tuple[list, list]:
    columns = [
        ("Name", "name"),
        ("CIDR", "cidr"),
        ("Version", "version"),
        ("Gateway", "gateway"),
        ("Utilization", "utilization"),
    ]
    rows = []
    pools = (
        db.query(IpPool)
        .order_by(IpPool.is_active.desc(), IpPool.name)
        .limit(_LIMIT)
        .all()
    )
    for p in pools:
        used, total = live_pool_counts(db, p)
        pct = f"{(used / total * 100):.0f}%" if total else "—"
        rows.append(
            {
                "id": str(p.id),
                "name": p.name,
                "cidr": p.cidr,
                "version": getattr(p.ip_version, "value", str(p.ip_version)),
                "gateway": p.gateway or "—",
                "utilization": f"{used} / {total} ({pct})" if total else f"{used} / 0",
            }
        )
    return columns, rows


def _block_rows(db: Session) -> tuple[list, list]:
    columns = [("CIDR", "cidr"), ("Pool", "pool"), ("Active", "active")]
    rows = [
        {
            "id": str(block.id),
            "cidr": block.cidr,
            "pool": pool_name,
            "active": "Yes" if block.is_active else "No",
        }
        for block, pool_name in (
            db.query(IpBlock, IpPool.name)
            .join(IpPool, IpBlock.pool_id == IpPool.id)
            .order_by(IpPool.name)
            .limit(_LIMIT)
            .all()
        )
    ]
    return columns, rows


def _prefix_rows(db: Session) -> tuple[list, list]:
    columns = [
        ("Prefix", "prefix"),
        ("Status", "__status"),
        ("Subscriber", "subscriber"),
    ]
    rows = [
        {
            "id": str(pfx.id),
            "prefix": f"{pfx.prefix}/{pfx.prefix_length}",
            "status": ipv6_prefix_state_presentation(pfx.state),
            "subscriber": str(pfx.subscriber_id) if pfx.subscriber_id else "—",
        }
        for pfx in (
            db.query(Ipv6DelegatedPrefix)
            .order_by(Ipv6DelegatedPrefix.created_at.desc())
            .limit(_LIMIT)
            .all()
        )
    ]
    return columns, rows


_DISPATCH = {
    "pools": _pool_rows,
    "blocks": _block_rows,
    "ipv6_prefixes": _prefix_rows,
}


def ipam_ledger_data(db: Session, facet: str = "pools") -> dict:
    """Return the ledger page data for one IPAM facet."""
    facet = facet if facet in _VALID else "pools"
    columns, rows = _DISPATCH[facet](db)
    return {
        "facet": facet,
        "facet_label": dict(FACETS)[facet],
        "facets": [{"key": k, "label": lbl} for k, lbl in FACETS],
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "detail_base": "",
    }
