"""Celery tasks for inbound CRM ticket synchronization."""

from __future__ import annotations

from datetime import timedelta

from app.celery_app import celery_app
from app.db import task_session
from app.services.crm_ticket_pull import (
    latest_crm_updated_at,
    pull_tickets,
    sync_ticket_by_id,
)

# Overlap margin on the incremental watermark: tolerates clock skew between
# the CRM and us, and tickets updated while a previous run was paging.
WATERMARK_MARGIN = timedelta(minutes=10)


@celery_app.task(name="app.tasks.crm_ticket_pull.pull_crm_tickets")
def pull_crm_tickets(limit: int = 200, max_pages: int = 50, full: bool = False) -> dict:
    with task_session() as db:
        since = None
        if not full:
            watermark = latest_crm_updated_at(db)
            if watermark:
                since = watermark - WATERMARK_MARGIN
        result = pull_tickets(db, limit=limit, max_pages=max_pages, since=since)
        return {
            "mode": "incremental" if since else "full",
            "since": since.isoformat() if since else None,
            **result.as_dict(),
        }


@celery_app.task(name="app.tasks.crm_ticket_pull.sync_crm_ticket")
def sync_crm_ticket(crm_ticket_id: str) -> dict:
    with task_session() as db:
        return sync_ticket_by_id(db, crm_ticket_id).as_dict()
