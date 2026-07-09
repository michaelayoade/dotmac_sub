"""Merged field schedule timeline.

Work-order job headers can be imported into ``work_order_mirror`` during
migration, while native field execution events, shifts, and availability are
authored in sub.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.dispatch import AvailabilityBlock, Shift
from app.models.work_order_mirror import WorkOrderMirror
from app.services.field.jobs import _profile_from_principal, _scoped_query

_DEFAULT_WINDOW_DAYS = 7
_MAX_WINDOW_DAYS = 31


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _window(
    date_from: datetime | None,
    date_to: datetime | None,
) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    start = (
        _as_utc(date_from)
        if date_from
        else now.replace(hour=0, minute=0, second=0, microsecond=0)
    )
    end = _as_utc(date_to) if date_to else start + timedelta(days=_DEFAULT_WINDOW_DAYS)
    if end <= start:
        raise HTTPException(status_code=422, detail="'to' must be after 'from'")
    if (end - start) > timedelta(days=_MAX_WINDOW_DAYS):
        end = start + timedelta(days=_MAX_WINDOW_DAYS)
    return start, end


class FieldSchedule:
    @staticmethod
    def timeline(
        db: Session,
        principal: dict[str, Any],
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[dict]:
        profile = _profile_from_principal(db, principal)
        start, end = _window(date_from, date_to)
        entries: list[dict] = []

        shifts = (
            db.query(Shift)
            .filter(Shift.technician_id == profile.id)
            .filter(Shift.is_active.is_(True))
            .filter(Shift.end_at >= start)
            .filter(Shift.start_at <= end)
            .all()
        )
        entries.extend(
            {
                "type": "shift",
                "start_at": _as_utc(shift.start_at),
                "end_at": _as_utc(shift.end_at),
                "title": shift.shift_type or "Shift",
                "reference_id": str(shift.id),
            }
            for shift in shifts
        )

        blocks = (
            db.query(AvailabilityBlock)
            .filter(AvailabilityBlock.technician_id == profile.id)
            .filter(AvailabilityBlock.is_active.is_(True))
            .filter(AvailabilityBlock.end_at >= start)
            .filter(AvailabilityBlock.start_at <= end)
            .all()
        )
        entries.extend(
            {
                "type": "availability",
                "start_at": _as_utc(block.start_at),
                "end_at": _as_utc(block.end_at),
                "title": block.reason or block.block_type or "Unavailable",
                "reference_id": str(block.id),
            }
            for block in blocks
        )

        jobs = (
            _scoped_query(db, profile)
            .filter(WorkOrderMirror.scheduled_start.isnot(None))
            .filter(WorkOrderMirror.scheduled_start >= start)
            .filter(WorkOrderMirror.scheduled_start <= end)
            .all()
        )
        entries.extend(
            {
                "type": "job",
                "start_at": _as_utc(row.scheduled_start),
                "end_at": _as_utc(row.scheduled_end) if row.scheduled_end else None,
                "title": row.title,
                "reference_id": row.crm_work_order_id,
            }
            for row in jobs
        )

        entries.sort(key=lambda item: item["start_at"])
        return entries


field_schedule = FieldSchedule()
