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


def run_crm_ticket_pull(db: Session, job: IntegrationJob, run_id) -> dict[str, Any]:
    client = _crm_client_from_job(job)
    filter_config = job.filter_config or {}
    page_size = int(filter_config.get("page_size") or filter_config.get("limit") or 200)
    max_pages = int(filter_config.get("max_pages") or 50)
    sync_comments = bool(filter_config.get("sync_comments", True))

    local_by_splynx = load_local_subscriber_map(db)
    subscriber_cache = build_subscriber_cache_from_map(local_by_splynx, client)

    def record_ticket(
        crm_ticket: dict[str, Any],
        action: str,
        comments_created: int,
        error: str | None,
        local_ticket_id,
    ) -> None:
        db.add(
            IntegrationRecord(
                run_id=run_id,
                entity_type=job.entity_type or "ticket",
                direction=job.direction or "pull",
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

    result = pull_tickets(
        db,
        client=client,
        limit=page_size,
        max_pages=max_pages,
        sync_comments=sync_comments,
        subscriber_cache=subscriber_cache,
        local_by_splynx=local_by_splynx,
        record_callback=record_ticket,
    )
    return result.as_dict()


def run_sync_job(db: Session, job: IntegrationJob, run_id) -> dict[str, Any] | None:
    adapter_key = (job.adapter_key or "").strip().lower()
    action = (job.action or "").strip().lower()
    if adapter_key == "crm" and action == "pull_tickets":
        return run_crm_ticket_pull(db, job, run_id)
    if not adapter_key and not action:
        return None
    raise SyncAdapterError(f"No sync adapter registered for {adapter_key}:{action}")
