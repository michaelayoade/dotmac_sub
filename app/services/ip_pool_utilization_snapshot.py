"""IP pool utilization snapshots — periodic point-in-time capture.

Live utilization is computed on demand (current counts only). This service
writes one ``IpPoolUtilizationSnapshot`` row per active pool so the admin UI
can chart utilization over time. Counts are derived independently of the live
``_build_pool_and_block_utilization`` helper to keep the snapshot stable.
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import (
    IpPool,
    IpPoolUtilizationSnapshot,
    IPv4Address,
    IPv6Address,
)

logger = logging.getLogger(__name__)

# Integer column ceiling — IPv6 pools have astronomically large capacity, so
# cap ``total`` to keep it in range. IPv6 percent is not meaningful by
# capacity anyway; the chart is primarily useful for IPv4 pools.
_TOTAL_CAP = 2_000_000_000

# Default chart window: how many of the most recent snapshots to plot.
# At the daily snapshot cadence this is ~1 year of trend.
_DEFAULT_HISTORY_LIMIT = 365

# Default retention: snapshots older than this are pruned. Kept a little
# beyond the chart window so the full window always has data.
_DEFAULT_RETENTION_DAYS = 400


def _ip_version(pool) -> str:
    raw = getattr(pool, "ip_version", None)
    return getattr(raw, "value", raw) or "ipv4"


def _capacity_from_cidr(cidr: str) -> int:
    try:
        network = ipaddress.ip_network(str(cidr or ""), strict=False)
    except ValueError:
        return 0
    total = network.num_addresses
    # Exclude network + broadcast for routed IPv4 blocks (/31, /32 keep all).
    if network.version == 4 and network.prefixlen < 31:
        total = max(total - 2, 0)
    return min(total, _TOTAL_CAP)


class IpPoolUtilizationSnapshotManager:
    """Captures and queries IP pool utilization snapshots."""

    @staticmethod
    def take_snapshot(
        db: Session, captured_at: datetime | None = None
    ) -> dict[str, int]:
        captured_at = captured_at or datetime.now(UTC)
        created = 0

        pools = list(db.scalars(select(IpPool).where(IpPool.is_active.is_(True))).all())
        for pool in pools:
            is_ipv6 = _ip_version(pool) == "ipv6"
            address_model = IPv6Address if is_ipv6 else IPv4Address

            used = (
                db.execute(
                    select(func.count(address_model.id)).where(
                        address_model.pool_id == pool.id,
                        address_model.assignment.has(is_active=True),
                    )
                ).scalar()
                or 0
            )
            reserved = (
                db.execute(
                    select(func.count(address_model.id)).where(
                        address_model.pool_id == pool.id,
                        address_model.is_reserved.is_(True),
                    )
                ).scalar()
                or 0
            )
            total = _capacity_from_cidr(getattr(pool, "cidr", ""))
            available = max(total - used - reserved, 0)
            percent = round(used / total * 100) if total else 0

            db.add(
                IpPoolUtilizationSnapshot(
                    pool_id=pool.id,
                    captured_at=captured_at,
                    total=total,
                    used=used,
                    reserved=reserved,
                    available=available,
                    percent=percent,
                )
            )
            created += 1

        db.commit()
        logger.info("IP pool utilization snapshot complete: pools=%d", created)
        return {"created": created}

    @staticmethod
    def history(
        db: Session, pool_id: str, *, limit: int = _DEFAULT_HISTORY_LIMIT
    ) -> list[IpPoolUtilizationSnapshot]:
        """Most recent snapshots for a pool, oldest-first for charting."""
        rows = list(
            db.scalars(
                select(IpPoolUtilizationSnapshot)
                .where(IpPoolUtilizationSnapshot.pool_id == pool_id)
                .order_by(IpPoolUtilizationSnapshot.captured_at.desc())
                .limit(limit)
            ).all()
        )
        return list(reversed(rows))

    @staticmethod
    def prune(
        db: Session, *, keep_days: int = _DEFAULT_RETENTION_DAYS
    ) -> dict[str, int]:
        """Delete snapshots older than ``keep_days``.

        The table is otherwise append-only and unbounded; a periodic prune
        keeps it from growing forever. Returns ``{"deleted": n}``.
        """
        cutoff = datetime.now(UTC) - timedelta(days=keep_days)
        deleted = (
            db.query(IpPoolUtilizationSnapshot)
            .filter(IpPoolUtilizationSnapshot.captured_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        logger.info(
            "Pruned %d IP pool utilization snapshot(s) older than %d days",
            deleted,
            keep_days,
        )
        return {"deleted": deleted}


ip_pool_utilization_snapshots = IpPoolUtilizationSnapshotManager()
