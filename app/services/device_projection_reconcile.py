"""Reconciler for the unified device projection (``device_projections``).

``network.device_projection`` is the sole canonical writer of the
``device_projections`` table. This reconciler is that writer: it runs the
authoritative multi-source device derivation (:func:`collect_devices`, which
aggregates OLTs, core ``NetworkDevice`` rows, ONTs and CPEs and derives each
one's operational status) and projects the result into one materialised row per
device.

The pass is idempotent and self-healing:

* every derived device is upserted on its ``(device_type, source_id)`` natural
  key, so re-running with unchanged inputs converges to the same rows;
* ``refreshed_at`` is stamped on every upserted row, carrying freshness;
* rows whose source device no longer exists are pruned, so the table cannot
  drift into holding phantom devices.

The table is a rebuildable cache — the authoritative device tables remain the
source of truth. Callers that need a live device list read the projection; they
never write it, and they request a reconcile rather than maintaining a parallel
derivation path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network_monitoring import DeviceProjection
from app.services.common import coerce_uuid
from app.services.web_network_core_devices_inventory import collect_devices

logger = logging.getLogger(__name__)

# Columns copied verbatim from a derived device dict onto its projection row.
_PROJECTED_FIELDS = (
    "name",
    "serial_number",
    "ip_address",
    "vendor",
    "model",
    "operational_reason",
    "last_seen",
)


@dataclass(frozen=True)
class DeviceProjectionReconcileResult:
    """Outcome of a single reconcile pass."""

    inserted: int
    updated: int
    pruned: int

    @property
    def total(self) -> int:
        """Rows present in the projection after this pass."""
        return self.inserted + self.updated


def _subscriber_id(subscriber: object) -> object | None:
    """Best-effort extraction of a subscriber UUID from the derived dict.

    The derivation currently carries ``None`` for every device, but accept an
    id or an object with an ``id`` so the projection keeps working if the
    derivation starts linking CPE to subscribers.
    """
    if subscriber is None:
        return None
    candidate = getattr(subscriber, "id", subscriber)
    return coerce_uuid(candidate)


def reconcile_device_projections(
    db: Session, *, now: datetime | None = None
) -> DeviceProjectionReconcileResult:
    """Rebuild ``device_projections`` from the authoritative device tables.

    Idempotent: safe to run on any schedule. Returns a summary of what changed.
    """
    stamp = now or datetime.now(UTC)

    existing: dict[tuple[str, str], DeviceProjection] = {
        (row.device_type, row.source_id): row
        for row in db.execute(select(DeviceProjection)).scalars()
    }

    seen: set[tuple[str, str]] = set()
    inserted = 0
    updated = 0

    for device in collect_devices(db):
        device_type = str(device["type"])
        source_id = str(device["id"])
        key = (device_type, source_id)
        seen.add(key)

        status = str(device.get("status") or "unknown")
        subscriber_id = _subscriber_id(device.get("subscriber"))

        row = existing.get(key)
        if row is None:
            row = DeviceProjection(
                device_type=device_type,
                source_id=source_id,
                operational_status=status,
                subscriber_id=subscriber_id,
                refreshed_at=stamp,
            )
            for field in _PROJECTED_FIELDS:
                setattr(row, field, device.get(field))
            db.add(row)
            inserted += 1
        else:
            row.operational_status = status
            row.subscriber_id = subscriber_id
            row.refreshed_at = stamp
            for field in _PROJECTED_FIELDS:
                setattr(row, field, device.get(field))
            updated += 1

    pruned = 0
    for key, row in existing.items():
        if key not in seen:
            db.delete(row)
            pruned += 1

    db.commit()

    logger.info(
        "device_projection reconcile: %d inserted, %d updated, %d pruned",
        inserted,
        updated,
        pruned,
    )
    return DeviceProjectionReconcileResult(
        inserted=inserted, updated=updated, pruned=pruned
    )
