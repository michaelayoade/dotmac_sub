"""Capability-only integration sync orchestration."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.integration import (
    IntegrationJob,
    IntegrationRecord,
    IntegrationRun,
    IntegrationRunStatus,
)
from app.models.integration_platform import (
    IntegrationCapabilityBinding,
    IntegrationCheckpoint,
)
from app.services.crm_ticket_pull import (
    WATERMARK_MARGIN,
    latest_crm_updated_at,
    pull_tickets,
)
from app.services.integrations.connectors.dotmac_crm import (
    CRM_TICKET_OBSERVATION_CAPABILITY,
    CrmTicketObservationSource,
    DotmacCrmRunner,
)
from app.services.integrations.runtime import OperationTrigger
from app.services.integrations.runtime_execution import (
    build_execution_context,
    crm_observation_source,
)

logger = logging.getLogger(__name__)


class SyncAdapterError(RuntimeError):
    """Raised when a sync job lacks an enabled typed capability."""


def _value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
    run_id: UUID,
    *,
    entity_type: str = "ticket",
    direction: str = "pull",
    skip_actions: tuple[str, ...] = ("unchanged",),
):
    """Return a change-only per-ticket IntegrationRecord callback."""

    def record_ticket(
        crm_ticket: dict[str, Any],
        action: str,
        comments_created: int,
        error: str | None,
        local_ticket_id: UUID | None,
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


def require_crm_ticket_capability_binding(
    job: IntegrationJob,
) -> IntegrationCapabilityBinding:
    binding = job.capability_binding
    if binding is None:
        raise SyncAdapterError("CRM sync job has no capability binding")
    if binding.capability_id != CRM_TICKET_OBSERVATION_CAPABILITY:
        raise SyncAdapterError("CRM sync job is bound to an incompatible capability")
    return binding


def _pin_run_to_binding(
    db: Session, run_id: UUID, binding: IntegrationCapabilityBinding
) -> IntegrationRun:
    run = db.get(IntegrationRun, run_id)
    if run is None:
        raise SyncAdapterError("integration run not found")
    installation = binding.installation
    revision = installation.current_config_revision
    if revision is None:
        raise SyncAdapterError("integration configuration revision is missing")
    run.installation_id = installation.id
    run.capability_binding_id = binding.id
    run.config_revision_id = revision.id
    run.capability_id = binding.capability_id
    run.connector_key = installation.connector_key
    run.connector_version = installation.connector_version
    run.manifest_digest = installation.manifest_digest
    db.flush()
    return run


def _runtime_source(
    db: Session,
    *,
    binding: IntegrationCapabilityBinding,
    run_id: UUID,
    trigger: OperationTrigger,
    client_override: CrmTicketObservationSource | None,
) -> CrmTicketObservationSource:
    runner_override = DotmacCrmRunner(client_override) if client_override else None
    context_kwargs: dict[str, Any] = {
        "capability_binding_id": binding.id,
        "runner_override": runner_override,
    }
    if client_override is not None:
        context_kwargs["secret_resolver"] = lambda _reference: "test-client-override"
    context = build_execution_context(db, **context_kwargs)
    return crm_observation_source(
        context,
        correlation_id=f"integration-run:{run_id}",
        trigger=trigger,
        actor="integration.sync",
    )


def run_crm_ticket_pull(
    db: Session,
    job: IntegrationJob,
    run_id: UUID,
    *,
    client: CrmTicketObservationSource | None = None,
    limit: int | None = None,
    max_pages: int | None = None,
    sync_comments: bool | None = None,
    since: datetime | None = None,
    trigger: OperationTrigger = OperationTrigger.manual,
) -> dict[str, Any]:
    binding = require_crm_ticket_capability_binding(job)
    _pin_run_to_binding(db, run_id, binding)
    filter_config = job.filter_config or {}
    page_size = int(
        limit or filter_config.get("page_size") or filter_config.get("limit") or 200
    )
    effective_max_pages = int(max_pages or filter_config.get("max_pages") or 50)
    effective_sync_comments = (
        bool(filter_config.get("sync_comments", True))
        if sync_comments is None
        else sync_comments
    )
    source = _runtime_source(
        db,
        binding=binding,
        run_id=run_id,
        trigger=trigger,
        client_override=client,
    )
    result = pull_tickets(
        db,
        client=source,
        limit=page_size,
        max_pages=effective_max_pages,
        since=since,
        sync_comments=effective_sync_comments,
        record_callback=make_ticket_recorder(
            db,
            run_id,
            entity_type=job.entity_type or "ticket",
            direction=job.direction or "pull",
        ),
    )
    return {
        "capability_id": binding.capability_id,
        "page_size": page_size,
        "max_pages": effective_max_pages,
        "sync_comments": effective_sync_comments,
        **result.as_dict(),
    }


_SYNC_CAPABILITY_HANDLERS = {
    CRM_TICKET_OBSERVATION_CAPABILITY: run_crm_ticket_pull,
}


def run_sync_job(
    db: Session, job: IntegrationJob, run_id: UUID
) -> dict[str, Any] | None:
    if job.capability_binding is None:
        raise SyncAdapterError("integration job has no capability binding")
    handler = _SYNC_CAPABILITY_HANDLERS.get(job.capability_binding.capability_id)
    if handler is None:
        raise SyncAdapterError(
            "No sync handler registered for capability "
            f"{job.capability_binding.capability_id}"
        )
    return handler(db, job, run_id)


def _checkpoint_watermark(db: Session, job: IntegrationJob) -> datetime | None:
    if job.capability_binding_id is None:
        return None
    checkpoint = (
        db.query(IntegrationCheckpoint)
        .filter(
            IntegrationCheckpoint.job_id == job.id,
            IntegrationCheckpoint.capability_binding_id == job.capability_binding_id,
        )
        .one_or_none()
    )
    raw = (checkpoint.cursor_json or {}).get("watermark") if checkpoint else None
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _advance_checkpoint(
    db: Session,
    *,
    job: IntegrationJob,
    run_id: UUID,
    watermark: datetime | None,
) -> None:
    if job.capability_binding_id is None or watermark is None:
        return
    checkpoint = (
        db.query(IntegrationCheckpoint)
        .filter(
            IntegrationCheckpoint.job_id == job.id,
            IntegrationCheckpoint.capability_binding_id == job.capability_binding_id,
        )
        .one_or_none()
    )
    if checkpoint is None:
        checkpoint = IntegrationCheckpoint(
            job_id=job.id,
            capability_binding_id=job.capability_binding_id,
            version=1,
            cursor_json={},
        )
        db.add(checkpoint)
    else:
        checkpoint.version += 1
    checkpoint.cursor_json = {"watermark": watermark.isoformat()}
    checkpoint.last_run_id = run_id
    checkpoint.advanced_at = datetime.now(UTC)
    checkpoint.updated_by = "integration.sync"
    db.flush()


def run_scheduled_pull(
    db: Session,
    *,
    client: CrmTicketObservationSource | None = None,
    limit: int = 200,
    max_pages: int = 50,
    full: bool = False,
) -> dict[str, Any]:
    """Run the enabled CRM capability with pinned run/checkpoint evidence."""

    job = (
        db.query(IntegrationJob)
        .join(
            IntegrationCapabilityBinding,
            IntegrationJob.capability_binding_id == IntegrationCapabilityBinding.id,
        )
        .filter(
            IntegrationJob.is_active.is_(True),
            IntegrationCapabilityBinding.capability_id
            == CRM_TICKET_OBSERVATION_CAPABILITY,
        )
        .first()
    )
    if job is None:
        raise SyncAdapterError("no active CRM ticket capability job configured")
    require_crm_ticket_capability_binding(job)
    filter_config = job.filter_config or {}
    limit = int(filter_config.get("page_size") or filter_config.get("limit") or limit)
    max_pages = int(filter_config.get("max_pages") or max_pages)
    sync_comments = bool(filter_config.get("sync_comments", True))
    since = None
    if not full:
        watermark = _checkpoint_watermark(db, job) or latest_crm_updated_at(db)
        if watermark:
            since = watermark - WATERMARK_MARGIN

    run = IntegrationRun(
        job_id=job.id,
        status=IntegrationRunStatus.running,
        trigger="scheduled_full" if full else "scheduled",
        requested_by="celery-beat",
    )
    db.add(run)
    db.flush()
    try:
        metrics = run_crm_ticket_pull(
            db,
            job,
            run.id,
            client=client,
            limit=limit,
            max_pages=max_pages,
            sync_comments=sync_comments,
            since=since,
            trigger=OperationTrigger.scheduled,
        )
    except Exception as exc:
        run.status = IntegrationRunStatus.failed
        run.error = str(exc)
        run.finished_at = datetime.now(UTC)
        db.commit()
        raise

    metrics.update(
        {
            "mode": "incremental" if since else "full",
            "since": since.isoformat() if since else None,
        }
    )
    errors = metrics.get("errors") or []
    metrics["partial_success"] = bool(errors and metrics.get("fetched"))
    run.status = (
        IntegrationRunStatus.failed
        if errors and not metrics.get("fetched")
        else IntegrationRunStatus.success
    )
    run.metrics = metrics
    if errors and metrics.get("fetched"):
        run.error = f"Completed with {len(errors)} per-ticket error(s)."
    run.finished_at = datetime.now(UTC)
    job.last_run_at = run.finished_at
    if not errors:
        _advance_checkpoint(
            db,
            job=job,
            run_id=run.id,
            watermark=latest_crm_updated_at(db),
        )
    db.commit()

    if errors:
        logger.warning(
            "crm_ticket_pull_errors count=%d first=%s", len(errors), errors[0]
        )
    if since and metrics.get("skipped_unmapped_subscribers"):
        logger.warning(
            "crm_ticket_pull_unmapped_subscribers count=%d (incremental run)",
            metrics["skipped_unmapped_subscribers"],
        )
    return metrics
