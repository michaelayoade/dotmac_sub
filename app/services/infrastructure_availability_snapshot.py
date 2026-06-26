"""Daily availability snapshots for infrastructure elements.

Writes one ``AvailabilitySnapshot`` row per element (device / pop_site /
pon_port) per day so the performance dashboard can chart availability trends
without re-merging the whole alert history on every render. Mirrors
``ip_pool_utilization_snapshot``. See INFRASTRUCTURE_SLA_PERFORMANCE.md Phase 2.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.network_monitoring import AvailabilitySnapshot
from app.schemas.network_monitoring import UptimeReportRequest
from app.services import network_monitoring as network_monitoring_service
from app.services import web_network_performance as perf

RETENTION_DAYS = 400

# engine group_by -> snapshot element_type
_GROUP_TO_ELEMENT = {
    "device": "device",
    "pop_site": "pop_site",
    "pon": "pon_port",
}


def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def take_snapshot(db: Session, day: datetime | None = None) -> dict[str, int | str]:
    """Snapshot availability for the given UTC day (default: yesterday).

    Idempotent per (element_type, element_id, day): re-running overwrites the
    day's rows rather than duplicating them.
    """
    if day is None:
        day = datetime.now(UTC) - timedelta(days=1)
    if day.tzinfo is None:  # never reinterpret a naive datetime as local time
        day = day.replace(tzinfo=UTC)
    period_start, period_end = _day_bounds(day.astimezone(UTC))

    device_meta = perf._device_meta(db)
    site_index = perf._site_device_index(device_meta)
    incidents = perf._incident_counts_by_device(db, period_start, period_end)
    pon_subs = perf._pon_subscriber_counts(db)

    created = 0
    for group_by, element_type in _GROUP_TO_ELEMENT.items():
        report = network_monitoring_service.uptime_report(
            db,
            UptimeReportRequest(
                period_start=period_start,
                period_end=period_end,
                group_by=group_by,
            ),
        )
        for item in report.items:
            if item.group_id is None:
                continue
            # blast radius + incident count per element type
            if element_type == "device":
                dev_ids = [str(item.group_id)]
            elif element_type == "pop_site":
                dev_ids = site_index.get(str(item.group_id), [])
            else:  # pon_port
                dev_ids = []

            if element_type == "pon_port":
                affected = pon_subs.get(str(item.group_id), 0)
                incident_count = 0
            else:
                affected = sum(device_meta.get(d, {}).get("subs", 0) for d in dev_ids)
                incident_count = sum(incidents.get(d, 0) for d in dev_ids)

            _upsert(
                db,
                element_type=element_type,
                element_id=item.group_id,
                snapshot_date=period_start,
                uptime_percent=(
                    float(item.uptime_percent)
                    if item.uptime_percent is not None
                    else None
                ),
                downtime_seconds=item.downtime_seconds,
                # Use the engine's total_seconds (device-scaled for pop_site:
                # window × device_count) so downtime_seconds ≤ window_seconds
                # holds for multi-device sites. uptime_percent already matches.
                window_seconds=item.total_seconds,
                incident_count=incident_count,
                affected_subscribers_peak=affected,
            )
            created += 1
    db.flush()
    return {"created": created, "day": period_start.date().isoformat()}


def _upsert(
    db: Session,
    *,
    element_type: str,
    element_id,
    snapshot_date: datetime,
    uptime_percent: float | None,
    downtime_seconds: int,
    window_seconds: int,
    incident_count: int,
    affected_subscribers_peak: int | None,
) -> None:
    existing = (
        db.query(AvailabilitySnapshot)
        .filter(
            AvailabilitySnapshot.element_type == element_type,
            AvailabilitySnapshot.element_id == element_id,
            AvailabilitySnapshot.snapshot_date == snapshot_date,
        )
        .first()
    )
    if existing is None:
        db.add(
            AvailabilitySnapshot(
                element_type=element_type,
                element_id=element_id,
                snapshot_date=snapshot_date,
                uptime_percent=uptime_percent,
                downtime_seconds=downtime_seconds,
                window_seconds=window_seconds,
                incident_count=incident_count,
                affected_subscribers_peak=affected_subscribers_peak,
            )
        )
    else:
        existing.uptime_percent = uptime_percent
        existing.downtime_seconds = downtime_seconds
        existing.window_seconds = window_seconds
        existing.incident_count = incident_count
        existing.affected_subscribers_peak = affected_subscribers_peak


def prune(db: Session, retention_days: int = RETENTION_DAYS) -> dict[str, int]:
    """Delete snapshots older than the retention window."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = (
        db.query(AvailabilitySnapshot)
        .filter(AvailabilitySnapshot.snapshot_date < cutoff)
        .delete(synchronize_session=False)
    )
    db.flush()
    return {"deleted": int(deleted or 0)}


def trend(db: Session, element_type: str, element_id, *, days: int = 365) -> list[dict]:
    """Availability trend points (oldest first) for one element."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = (
        db.query(AvailabilitySnapshot)
        .filter(
            AvailabilitySnapshot.element_type == element_type,
            AvailabilitySnapshot.element_id == element_id,
            AvailabilitySnapshot.snapshot_date >= cutoff,
        )
        .order_by(AvailabilitySnapshot.snapshot_date.asc())
        .all()
    )
    return [
        {
            "date": r.snapshot_date.date().isoformat(),
            "uptime_percent": r.uptime_percent,
            "downtime_seconds": r.downtime_seconds,
            "incident_count": r.incident_count,
        }
        for r in rows
    ]
