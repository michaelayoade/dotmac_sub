"""Celery tasks for inbound CRM ticket synchronization."""

from __future__ import annotations

from app.celery_app import celery_app
from app.db import task_session
from app.services.crm_ticket_pull import pull_tickets, sync_ticket_by_id


@celery_app.task(name="app.tasks.crm_ticket_pull.pull_crm_tickets")
def pull_crm_tickets(limit: int = 200, max_pages: int = 50) -> dict:
    with task_session() as db:
        return pull_tickets(db, limit=limit, max_pages=max_pages).as_dict()


@celery_app.task(name="app.tasks.crm_ticket_pull.sync_crm_ticket")
def sync_crm_ticket(crm_ticket_id: str) -> dict:
    with task_session() as db:
        return sync_ticket_by_id(db, crm_ticket_id).as_dict()
