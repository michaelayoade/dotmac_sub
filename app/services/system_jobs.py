"""Shared helpers for persisted system job records."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.system_job import SystemJob


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    text = str(value or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    job_id = str(payload.get("job_id") or payload.get("id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    return {**payload, "job_id": job_id}


def _payload_owner_actor_id(payload: dict[str, Any]) -> str | None:
    for key in ("requested_by_id", "actor_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return None


def _payload_owner_email(payload: dict[str, Any]) -> str | None:
    for key in ("requested_by_email", "requested_by"):
        value = str(payload.get(key) or "").strip().lower()
        if value:
            return value
    return None


_TERMINAL_JOB_STATUSES = {"completed", "failed", "canceled", "cancelled"}


def _row_to_payload(row: SystemJob) -> dict[str, Any]:
    payload = dict(row.payload_json) if isinstance(row.payload_json, dict) else {}
    payload["job_id"] = row.job_id
    return payload


def list_jobs(
    db: Session,
    *,
    job_type: str,
    limit: int = 20,
    owner_actor_id: str | None = None,
    owner_email: str | None = None,
) -> list[dict[str, Any]]:
    query = db.query(SystemJob).filter(SystemJob.job_type == job_type)
    actor = str(owner_actor_id or "").strip()
    email = str(owner_email or "").strip().lower()
    if actor:
        query = query.filter(SystemJob.owner_actor_id == actor)
    elif email:
        query = query.filter(SystemJob.owner_actor_id.is_(None)).filter(
            SystemJob.owner_email == email
        )
    rows = query.order_by(SystemJob.updated_at.desc()).limit(max(1, limit)).all()
    return [_row_to_payload(row) for row in rows]


def get_job(
    db: Session,
    *,
    job_type: str,
    job_id: str,
    owner_actor_id: str | None = None,
    owner_email: str | None = None,
) -> dict[str, Any] | None:
    query = db.query(SystemJob).filter(SystemJob.job_type == job_type)
    query = query.filter(SystemJob.job_id == str(job_id or "").strip())
    actor = str(owner_actor_id or "").strip()
    email = str(owner_email or "").strip().lower()
    if actor:
        query = query.filter(SystemJob.owner_actor_id == actor)
    elif email:
        query = query.filter(SystemJob.owner_actor_id.is_(None)).filter(
            SystemJob.owner_email == email
        )
    row = query.first()
    if row is None:
        return None
    return _row_to_payload(row)


def upsert_job(
    db: Session, *, job_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    normalized = _normalize_payload(payload)
    row = (
        db.query(SystemJob)
        .filter(SystemJob.job_type == job_type)
        .filter(SystemJob.job_id == normalized["job_id"])
        .first()
    )
    if row is None:
        row = SystemJob(job_id=normalized["job_id"], job_type=job_type)
        db.add(row)
        merged_payload = dict(normalized)
    else:
        existing_payload = (
            dict(row.payload_json) if isinstance(row.payload_json, dict) else {}
        )
        merged_payload = {**existing_payload, **normalized}
        incoming_actor_id = _payload_owner_actor_id(normalized)
        incoming_email = _payload_owner_email(normalized)
        if (
            row.owner_actor_id
            and incoming_actor_id
            and row.owner_actor_id != incoming_actor_id
        ):
            raise HTTPException(
                status_code=403, detail="Job belongs to a different actor"
            )
        if (
            row.owner_actor_id is None
            and row.owner_email
            and incoming_email
            and row.owner_email != incoming_email
        ):
            raise HTTPException(
                status_code=403, detail="Job belongs to a different actor"
            )
        current_status = str(row.status or "").strip().lower()
        incoming_status = (
            str(merged_payload.get("status") or row.status or "").strip().lower()
        )
        if (
            current_status in _TERMINAL_JOB_STATUSES
            and incoming_status
            and incoming_status != current_status
        ):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot update terminal job in status '{row.status}'",
            )
    row.job_type = job_type
    row.status = str(merged_payload.get("status") or row.status or "queued")
    row.module = str(merged_payload.get("module") or row.module or "").strip() or None
    row.owner_actor_id = _payload_owner_actor_id(merged_payload)
    row.owner_email = _payload_owner_email(merged_payload)
    progress = merged_payload.get("progress_percent")
    row.progress_percent = int(progress) if progress is not None else None
    error = merged_payload.get("error")
    row.error = None if error in (None, "") else str(error)
    row.queued_at = _parse_datetime(merged_payload.get("queued_at"))
    row.started_at = _parse_datetime(merged_payload.get("started_at"))
    row.completed_at = _parse_datetime(merged_payload.get("completed_at"))
    row.payload_json = dict(merged_payload)
    db.commit()
    db.refresh(row)
    return _row_to_payload(row)
