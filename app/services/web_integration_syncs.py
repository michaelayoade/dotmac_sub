"""Web helpers for generic integration sync profiles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.connector import ConnectorAuthType, ConnectorConfig, ConnectorType
from app.models.integration import (
    IntegrationJob,
    IntegrationJobType,
    IntegrationRecord,
    IntegrationRun,
    IntegrationRunStatus,
    IntegrationScheduleType,
    IntegrationTarget,
    IntegrationTargetType,
)
from app.models.support import Ticket, TicketComment
from app.services.common import coerce_uuid
from app.services.web_integrations import _parse_json


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def ensure_default_crm_ticket_sync(db: Session) -> IntegrationJob:
    connector = (
        db.query(ConnectorConfig)
        .filter(ConnectorConfig.name == "DotMac CRM")
        .one_or_none()
    )
    if connector is None:
        connector = ConnectorConfig(
            name="DotMac CRM",
            connector_type=ConnectorType.http,
            base_url=settings.crm_base_url,
            auth_type=ConnectorAuthType.basic,
            auth_config={
                "username": settings.crm_username,
                "password": settings.crm_password,
            },
            timeout_sec=45,
            metadata_={"sync_adapter": "crm"},
            notes="Default CRM connector used by generic sync profiles.",
            is_active=True,
        )
        db.add(connector)
        db.flush()

    target = (
        db.query(IntegrationTarget)
        .filter(IntegrationTarget.name == "DotMac CRM")
        .one_or_none()
    )
    if target is None:
        target = IntegrationTarget(
            name="DotMac CRM",
            target_type=IntegrationTargetType.crm,
            connector_config_id=connector.id,
            notes="CRM target for generic ticket sync flows.",
            is_active=True,
        )
        db.add(target)
        db.flush()
    elif target.connector_config_id is None:
        target.connector_config_id = connector.id

    job = (
        db.query(IntegrationJob)
        .filter(IntegrationJob.adapter_key == "crm")
        .filter(IntegrationJob.action == "pull_tickets")
        .one_or_none()
    )
    if job is None:
        job = IntegrationJob(
            target_id=target.id,
            name="Pull CRM Tickets",
            job_type=IntegrationJobType.sync,
            schedule_type=IntegrationScheduleType.manual,
            interval_minutes=None,
            adapter_key="crm",
            action="pull_tickets",
            entity_type="ticket",
            direction="pull",
            trigger_mode="manual",
            mapping_config={
                "primary": "crm_subscriber legacy external_id",
                "fallback": "single structured customer ID pair in title/description",
                "ambiguous": "skip",
            },
            filter_config={"page_size": 200, "max_pages": 50, "sync_comments": True},
            conflict_policy="remote_wins",
            notes="Pulls CRM tickets into local support tickets using safe subscriber mapping.",
            is_active=True,
        )
        db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _job_runs(db: Session, job_ids: list) -> dict[str, list[IntegrationRun]]:
    if not job_ids:
        return {}
    rows = (
        db.query(IntegrationRun)
        .filter(IntegrationRun.job_id.in_(job_ids))
        .order_by(IntegrationRun.started_at.desc())
        .limit(200)
        .all()
    )
    grouped: dict[str, list[IntegrationRun]] = {}
    for run in rows:
        grouped.setdefault(str(run.job_id), []).append(run)
    return grouped


def _daily_counts(db: Session, days: int = 14) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        db.query(
            func.date(IntegrationRun.started_at).label("day"),
            IntegrationJob.direction,
            IntegrationRun.status,
            func.count(IntegrationRun.id),
        )
        .join(IntegrationJob, IntegrationJob.id == IntegrationRun.job_id)
        .filter(IntegrationRun.started_at >= since)
        .group_by("day", IntegrationJob.direction, IntegrationRun.status)
        .order_by("day")
        .all()
    )
    return [
        {
            "day": str(day),
            "direction": direction or "sync",
            "status": _enum_value(status),
            "count": count,
        }
        for day, direction, status, count in rows
    ]


def build_syncs_index_data(db: Session) -> dict[str, Any]:
    ensure_default_crm_ticket_sync(db)
    sync_jobs = (
        db.query(IntegrationJob)
        .filter(IntegrationJob.job_type == IntegrationJobType.sync)
        .order_by(IntegrationJob.name.asc())
        .all()
    )
    runs_by_job = _job_runs(db, [job.id for job in sync_jobs])
    failed_records = (
        db.query(IntegrationRecord)
        .filter(IntegrationRecord.status == "failed")
        .order_by(IntegrationRecord.created_at.desc())
        .limit(10)
        .all()
    )
    return {
        "sync_jobs": sync_jobs,
        "runs_by_job": runs_by_job,
        "daily_counts": _daily_counts(db),
        "failed_records": failed_records,
        "stats": {
            "total": len(sync_jobs),
            "active": sum(1 for job in sync_jobs if job.is_active),
            "pull": sum(1 for job in sync_jobs if job.direction == "pull"),
            "push": sum(1 for job in sync_jobs if job.direction == "push"),
        },
    }


def build_sync_detail_data(db: Session, job_id: str) -> dict[str, Any]:
    job = db.get(IntegrationJob, coerce_uuid(job_id))
    if job is None:
        raise ValueError("Sync profile not found")
    runs = (
        db.query(IntegrationRun)
        .filter(IntegrationRun.job_id == job.id)
        .order_by(IntegrationRun.started_at.desc())
        .limit(50)
        .all()
    )
    run_ids = [run.id for run in runs[:5]]
    records = []
    if run_ids:
        records = (
            db.query(IntegrationRecord)
            .filter(IntegrationRecord.run_id.in_(run_ids))
            .order_by(IntegrationRecord.created_at.desc())
            .limit(100)
            .all()
        )
    return {"job": job, "runs": runs, "records": records}


def update_sync_profile(
    db: Session,
    job_id: str,
    *,
    schedule_type: str,
    interval_minutes: str | None,
    trigger_mode: str | None,
    mapping_config: str | None,
    filter_config: str | None,
    page_size: str | None = None,
    max_pages: str | None = None,
    sync_comments: bool | None = None,
    mapping_primary: str | None = None,
    mapping_fallback: str | None = None,
    mapping_ambiguous: str | None = None,
    conflict_policy: str | None,
    is_active: bool,
) -> IntegrationJob:
    job = db.get(IntegrationJob, coerce_uuid(job_id))
    if job is None:
        raise ValueError("Sync profile not found")
    interval_value = int(interval_minutes) if interval_minutes else None
    if schedule_type == "interval" and not interval_value:
        raise ValueError("interval_minutes is required for interval schedules")
    job.schedule_type = IntegrationScheduleType(schedule_type)
    job.interval_minutes = interval_value
    job.trigger_mode = (trigger_mode or "").strip() or None
    if job.adapter_key == "crm" and job.action == "pull_tickets":
        job.mapping_config = {
            "primary": (mapping_primary or "").strip(),
            "fallback": (mapping_fallback or "").strip(),
            "ambiguous": (mapping_ambiguous or "").strip(),
        }
        job.filter_config = {
            "page_size": int(page_size or 200),
            "max_pages": int(max_pages or 50),
            "sync_comments": bool(sync_comments),
        }
    else:
        job.mapping_config = _parse_json(mapping_config, "mapping_config")
        job.filter_config = _parse_json(filter_config, "filter_config")
    job.conflict_policy = (conflict_policy or "").strip() or None
    job.is_active = is_active
    db.commit()
    db.refresh(job)
    return job


def backfill_crm_ticket_import_history(db: Session, job_id: str) -> IntegrationRun:
    job = db.get(IntegrationJob, coerce_uuid(job_id))
    if job is None:
        raise ValueError("Sync profile not found")

    existing = (
        db.query(IntegrationRun)
        .filter(IntegrationRun.job_id == job.id)
        .filter(IntegrationRun.trigger == "backfill")
        .first()
    )
    if existing:
        return existing

    crm_ticket_filter = Ticket.metadata_["sync_source"].as_string() == "crm"
    crm_comment_filter = TicketComment.metadata_["sync_source"].as_string() == "crm"
    tickets = (
        db.query(Ticket)
        .filter(crm_ticket_filter)
        .order_by(Ticket.created_at.asc())
        .all()
    )
    comments_count = (
        db.query(func.count(TicketComment.id)).filter(crm_comment_filter).scalar() or 0
    )

    started_at = min(
        (ticket.created_at for ticket in tickets if ticket.created_at),
        default=datetime.now(UTC),
    )
    run = IntegrationRun(
        job_id=job.id,
        status=IntegrationRunStatus.success,
        trigger="backfill",
        requested_by="system",
        started_at=started_at,
        finished_at=datetime.now(UTC),
        metrics={
            "fetched": len(tickets),
            "created": len(tickets),
            "updated": 0,
            "skipped_unmapped_subscribers": 0,
            "comments_created": comments_count,
            "errors": [],
            "source": "backfilled_from_existing_crm_ticket_import",
        },
    )
    db.add(run)
    db.flush()

    for ticket in tickets:
        metadata = ticket.metadata_ or {}
        db.add(
            IntegrationRecord(
                run_id=run.id,
                entity_type=job.entity_type or "ticket",
                direction=job.direction or "pull",
                local_id=str(ticket.id),
                remote_id=str(metadata.get("crm_ticket_id") or "") or None,
                remote_number=ticket.number,
                action="created",
                status="success",
                reason="Backfilled from existing CRM ticket import.",
                payload_snapshot={
                    "number": ticket.number,
                    "title": ticket.title,
                    "status": ticket.status,
                    "priority": ticket.priority,
                    "subscriber_id": (
                        str(ticket.subscriber_id) if ticket.subscriber_id else None
                    ),
                    "crm_ticket_id": metadata.get("crm_ticket_id"),
                    "crm_ticket_number": metadata.get("crm_ticket_number"),
                },
                created_at=ticket.created_at or datetime.now(UTC),
            )
        )

    job.last_run_at = run.finished_at
    db.commit()
    db.refresh(run)
    return run


def trigger_sync_job(job_id: str) -> None:
    from app.tasks.integrations import run_integration_job

    run_integration_job.delay(job_id, "manual")
