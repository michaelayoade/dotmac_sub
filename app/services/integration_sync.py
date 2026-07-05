"""Generic sync job dispatch and adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.integration import IntegrationJob, IntegrationRecord
from app.services.crm_client import CRMClient
from app.services.crm_ticket_pull import (
    build_subscriber_cache_from_map,
    load_local_subscriber_map,
    pull_tickets,
)


class SyncAdapterError(RuntimeError):
    """Raised when a sync job cannot be dispatched."""


def _value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _crm_client_from_job(job: IntegrationJob) -> CRMClient:
    connector = job.target.connector_config if job.target else None
    auth_config = connector.auth_config if connector and connector.auth_config else {}
    return CRMClient(
        base_url=_value(connector.base_url if connector else None)
        or settings.crm_base_url,
        username=_value(auth_config.get("username")) or settings.crm_username,
        password=_value(auth_config.get("password")) or settings.crm_password,
        service_token=_value(auth_config.get("service_token"))
        or settings.crm_service_token,
        timeout=float(
            connector.timeout_sec if connector and connector.timeout_sec else 45
        ),
    )


def _record_status(action: str) -> str:
    if action in {"created", "updated"}:
        return "success"
    if action.startswith("skipped"):
        return "skipped"
    return "failed" if action == "failed" else action


def _record_reason(action: str, error: str | None) -> str | None:
    if error:
        return error
    return {
        "skipped_lead": "CRM ticket is lead-only and has no subscriber.",
        "skipped_unmapped_subscriber": "No safe local subscriber mapping found.",
    }.get(action)


def _payload_snapshot(ticket: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": ticket.get("number"),
        "title": ticket.get("title"),
        "subscriber_id": ticket.get("subscriber_id"),
        "lead_id": ticket.get("lead_id"),
        "status": ticket.get("status"),
        "priority": ticket.get("priority"),
        "updated_at": ticket.get("updated_at"),
    }


def make_ticket_recorder(
    db: Session,
    run_id,
    *,
    entity_type: str = "ticket",
    direction: str = "pull",
    skip_actions: tuple[str, ...] = ("unchanged",),
):
    """Per-ticket IntegrationRecord callback for pull_tickets.

    Unchanged tickets are not recorded by default — a steady-state run would
    otherwise write thousands of no-op rows per day.
    """

    def record_ticket(
        crm_ticket: dict[str, Any],
        action: str,
        comments_created: int,
        error: str | None,
        local_ticket_id,
    ) -> None:
        if action in skip_actions:
            return
        db.add(
            IntegrationRecord(
                run_id=run_id,
                entity_type=entity_type,
                direction=direction,
                local_id=str(local_ticket_id) if local_ticket_id else None,
                remote_id=_value(crm_ticket.get("id")),
                remote_number=_value(crm_ticket.get("number")),
                action=action,
                status=_record_status(action),
                reason=_record_reason(action, error),
                payload_snapshot={
                    **_payload_snapshot(crm_ticket),
                    "comments_created": comments_created,
                },
                created_at=datetime.now(UTC),
            )
        )

    return record_ticket


def run_crm_ticket_pull(db: Session, job: IntegrationJob, run_id) -> dict[str, Any]:
    client = _crm_client_from_job(job)
    filter_config = job.filter_config or {}
    page_size = int(filter_config.get("page_size") or filter_config.get("limit") or 200)
    max_pages = int(filter_config.get("max_pages") or 50)
    sync_comments = bool(filter_config.get("sync_comments", True))

    local_by_splynx = load_local_subscriber_map(db)
    subscriber_cache = build_subscriber_cache_from_map(local_by_splynx, client)

    result = pull_tickets(
        db,
        client=client,
        limit=page_size,
        max_pages=max_pages,
        sync_comments=sync_comments,
        subscriber_cache=subscriber_cache,
        local_by_splynx=local_by_splynx,
        record_callback=make_ticket_recorder(
            db,
            run_id,
            entity_type=job.entity_type or "ticket",
            direction=job.direction or "pull",
        ),
    )
    return result.as_dict()


def run_scheduled_pull(
    db: Session,
    *,
    client: CRMClient | None = None,
    limit: int = 200,
    max_pages: int = 50,
    full: bool = False,
) -> dict[str, Any]:
    """Beat-scheduled CRM ticket pull with run history and alerting.

    Records an IntegrationRun (+ per-change IntegrationRecords) against the
    active crm/pull_tickets job so scheduled runs show up in the admin
    Integrations UI alongside manual ones. Falls back to an unrecorded pull
    when no such job is configured.
    """
    import logging

    from app.models.integration import IntegrationRun, IntegrationRunStatus
    from app.services.crm_ticket_pull import (
        WATERMARK_MARGIN,
        latest_crm_updated_at,
    )

    logger = logging.getLogger(__name__)

    since = None
    if not full:
        watermark = latest_crm_updated_at(db)
        if watermark:
            since = watermark - WATERMARK_MARGIN

    job = (
        db.query(IntegrationJob)
        .filter(
            IntegrationJob.adapter_key == "crm",
            IntegrationJob.action == "pull_tickets",
            IntegrationJob.is_active.is_(True),
        )
        .first()
    )
    if job is not None:
        filter_config = job.filter_config or {}
        limit = int(
            filter_config.get("page_size") or filter_config.get("limit") or limit
        )
        max_pages = int(filter_config.get("max_pages") or max_pages)
        sync_comments = bool(filter_config.get("sync_comments", True))
    else:
        sync_comments = True
    run = None
    record_callback = None
    if job is not None:
        run = IntegrationRun(
            job_id=job.id,
            status=IntegrationRunStatus.running,
            trigger="scheduled_full" if full else "scheduled",
            requested_by="celery-beat",
        )
        db.add(run)
        db.flush()
        record_callback = make_ticket_recorder(
            db,
            run.id,
            entity_type=job.entity_type or "ticket",
            direction=job.direction or "pull",
        )

    try:
        result = pull_tickets(
            db,
            client=client,
            limit=limit,
            max_pages=max_pages,
            since=since,
            sync_comments=sync_comments,
            record_callback=record_callback,
        )
    except Exception as exc:
        if run is not None:
            run.status = IntegrationRunStatus.failed
            run.error = str(exc)
            run.finished_at = datetime.now(UTC)
            db.commit()
        raise

    metrics = {
        "mode": "incremental" if since else "full",
        "since": since.isoformat() if since else None,
        "page_size": limit,
        "max_pages": max_pages,
        "sync_comments": sync_comments,
        **result.as_dict(),
    }
    metrics["partial_success"] = bool(result.errors and result.fetched)
    if run is not None:
        run.status = (
            IntegrationRunStatus.failed
            if result.errors and not result.fetched
            else IntegrationRunStatus.success
        )
        run.metrics = metrics
        if result.errors and result.fetched:
            run.error = f"Completed with {len(result.errors)} per-ticket error(s)."
        run.finished_at = datetime.now(UTC)
        if job is not None:
            job.last_run_at = run.finished_at
        db.commit()

    if result.errors:
        logger.warning(
            "crm_ticket_pull_errors count=%d first=%s",
            len(result.errors),
            result.errors[0],
        )
    if since and result.skipped_unmapped_subscribers:
        logger.warning(
            "crm_ticket_pull_unmapped_subscribers count=%d (incremental run)",
            result.skipped_unmapped_subscribers,
        )
    return metrics


def run_sync_job(db: Session, job: IntegrationJob, run_id) -> dict[str, Any] | None:
    adapter_key = (job.adapter_key or "").strip().lower()
    action = (job.action or "").strip().lower()
    if adapter_key == "crm" and action == "pull_tickets":
        return run_crm_ticket_pull(db, job, run_id)
    if not adapter_key and not action:
        return None
    raise SyncAdapterError(f"No sync adapter registered for {adapter_key}:{action}")
