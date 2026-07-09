"""Native field worklogs for imported work-order mirrors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.field_worklog import FieldWorkLog
from app.models.work_order_mirror import WorkOrderMirror
from app.services.field.jobs import _profile_from_principal, _scoped_query

_MAX_DURATION_HOURS = 16
_BACKDATED_FLAG_DAYS = 7


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _minutes(start_at: datetime, end_at: datetime | None) -> int:
    if end_at is None:
        return 0
    return max(0, int((_as_utc(end_at) - _as_utc(start_at)).total_seconds() // 60))


def _serialize(log: FieldWorkLog) -> dict:
    return {
        "id": log.id,
        "person_id": log.person_id,
        "start_at": log.start_at,
        "end_at": log.end_at,
        "minutes": log.minutes,
        "notes": log.notes,
    }


class FieldWorkLogs:
    @staticmethod
    def list_for_job(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
    ) -> list[dict]:
        row = _scoped_work_order(db, principal, crm_work_order_id)
        logs = (
            db.query(FieldWorkLog)
            .filter(FieldWorkLog.work_order_mirror_id == row.id)
            .filter(FieldWorkLog.is_active.is_(True))
            .order_by(FieldWorkLog.start_at.asc())
            .all()
        )
        return [_serialize(log) for log in logs]

    @staticmethod
    def submit(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
        entries: list[dict[str, Any]],
    ) -> list[dict]:
        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrderMirror.crm_work_order_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")

        now = datetime.now(UTC)
        results: list[dict] = []
        for entry in entries:
            start_at = entry.get("start_at")
            if not isinstance(start_at, datetime):
                raise HTTPException(status_code=422, detail="start_at is required")
            start_at = _as_utc(start_at)
            end_at = entry.get("end_at")
            end_at = _as_utc(end_at) if isinstance(end_at, datetime) else None

            if end_at is not None:
                if end_at <= start_at:
                    raise HTTPException(
                        status_code=422, detail="end_at must be after start_at"
                    )
                if (end_at - start_at) > timedelta(hours=_MAX_DURATION_HOURS):
                    raise HTTPException(
                        status_code=422,
                        detail=f"Worklog exceeds maximum duration of {_MAX_DURATION_HOURS} hours",
                    )

            client_ref = entry.get("client_ref")
            duplicate = _find_duplicate(
                db,
                person_id=profile.person_id,
                crm_work_order_id=row.crm_work_order_id,
                start_at=start_at,
                client_ref=client_ref,
            )
            if duplicate is not None:
                results.append(
                    {
                        "worklog": _serialize(duplicate),
                        "duplicate": True,
                        "backdated": False,
                    }
                )
                continue

            _check_overlap(db, profile.person_id, start_at, end_at)
            log = FieldWorkLog(
                work_order_mirror_id=row.id,
                crm_work_order_id=row.crm_work_order_id,
                author_technician_id=profile.id,
                person_id=profile.person_id,
                system_user_id=profile.system_user_id,
                start_at=start_at,
                end_at=end_at,
                minutes=_minutes(start_at, end_at),
                notes=entry.get("notes"),
                client_ref=client_ref,
            )
            db.add(log)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                duplicate = _find_duplicate(
                    db,
                    person_id=profile.person_id,
                    crm_work_order_id=row.crm_work_order_id,
                    start_at=start_at,
                    client_ref=client_ref,
                )
                if duplicate is None:
                    raise
                results.append(
                    {
                        "worklog": _serialize(duplicate),
                        "duplicate": True,
                        "backdated": False,
                    }
                )
                continue
            db.refresh(log)
            results.append(
                {
                    "worklog": _serialize(log),
                    "duplicate": False,
                    "backdated": (now - start_at)
                    > timedelta(days=_BACKDATED_FLAG_DAYS),
                }
            )
        return results


def _find_duplicate(
    db: Session,
    *,
    person_id,
    crm_work_order_id: str,
    start_at: datetime,
    client_ref,
) -> FieldWorkLog | None:
    if client_ref:
        existing = (
            db.query(FieldWorkLog)
            .filter(FieldWorkLog.client_ref == client_ref)
            .filter(FieldWorkLog.person_id == person_id)
            .one_or_none()
        )
        if existing is not None:
            return existing
    return (
        db.query(FieldWorkLog)
        .filter(FieldWorkLog.person_id == person_id)
        .filter(FieldWorkLog.crm_work_order_id == crm_work_order_id)
        .filter(FieldWorkLog.start_at == start_at)
        .filter(FieldWorkLog.is_active.is_(True))
        .first()
    )


def _check_overlap(
    db: Session,
    person_id,
    start_at: datetime,
    end_at: datetime | None,
) -> None:
    query = (
        db.query(FieldWorkLog)
        .filter(FieldWorkLog.person_id == person_id)
        .filter(FieldWorkLog.is_active.is_(True))
    )
    if end_at is None:
        if query.filter(FieldWorkLog.end_at.is_(None)).first():
            raise HTTPException(status_code=409, detail="A timer is already running")
        return

    candidates = query.filter(
        or_(FieldWorkLog.end_at.is_(None), FieldWorkLog.end_at > start_at)
    ).all()
    for log in candidates:
        log_start = _as_utc(log.start_at)
        log_end = _as_utc(log.end_at) if log.end_at else None
        if log_start < end_at and (log_end is None or log_end > start_at):
            raise HTTPException(
                status_code=409, detail="Worklog overlaps an existing entry"
            )


def _scoped_work_order(
    db: Session,
    principal: dict[str, Any],
    crm_work_order_id: str,
) -> WorkOrderMirror:
    profile = _profile_from_principal(db, principal)
    row = (
        _scoped_query(db, profile)
        .filter(WorkOrderMirror.crm_work_order_id == crm_work_order_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


field_worklogs = FieldWorkLogs()
