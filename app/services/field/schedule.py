"""Merged field schedule timeline.

Work-order job headers can be imported into ``work_order_mirror`` during
migration, while native field execution events, shifts, and availability are
authored in sub.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.dispatch import AvailabilityBlock, Shift
from app.models.work_order import WorkOrder
from app.schemas.field import FieldScheduleEntry
from app.services.field.execution_contracts import FieldJobsQuery
from app.services.field.work_order_access import (
    FieldAccessError,
    FieldActorKind,
    ResolveFieldActor,
    resolve_field_actor,
    scoped_work_orders,
)

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
        raise FieldAccessError(
            code="operations.field_work_order_access.invalid_request",
            message="'to' must be after 'from'",
        )
    if (end - start) > timedelta(days=_MAX_WINDOW_DAYS):
        end = start + timedelta(days=_MAX_WINDOW_DAYS)
    return start, end


class FieldSchedule:
    @staticmethod
    def timeline(db: Session, query: FieldJobsQuery) -> list[FieldScheduleEntry]:
        profile = resolve_field_actor(
            db, ResolveFieldActor(query.requester_system_user_id)
        )
        start, end = _window(query.date_from, query.date_to)
        entries: list[dict] = []

        shifts = (
            (
                db.query(Shift)
                .filter(Shift.technician_id == profile.technician_id)
                .filter(Shift.is_active.is_(True))
                .filter(Shift.end_at >= start)
                .filter(Shift.start_at <= end)
                .all()
            )
            if profile.kind == FieldActorKind.technician
            else []
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
            (
                db.query(AvailabilityBlock)
                .filter(AvailabilityBlock.technician_id == profile.technician_id)
                .filter(AvailabilityBlock.is_active.is_(True))
                .filter(AvailabilityBlock.end_at >= start)
                .filter(AvailabilityBlock.start_at <= end)
                .all()
            )
            if profile.kind == FieldActorKind.technician
            else []
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
            scoped_work_orders(db, profile)
            .filter(WorkOrder.scheduled_start.isnot(None))
            .filter(WorkOrder.scheduled_start >= start)
            .filter(WorkOrder.scheduled_start <= end)
            .all()
        )
        entries.extend(
            {
                "type": "job",
                "start_at": _as_utc(row.scheduled_start),
                "end_at": _as_utc(row.scheduled_end) if row.scheduled_end else None,
                "title": row.title,
                "reference_id": row.public_id,
            }
            for row in jobs
            if row.scheduled_start is not None
        )

        entries.sort(key=lambda item: item["start_at"])
        return [FieldScheduleEntry.model_validate(item) for item in entries]


field_schedule = FieldSchedule()
