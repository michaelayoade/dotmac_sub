"""Celery tasks for native support ticket workflows."""

from __future__ import annotations

from app.celery_app import celery_app
from app.db import task_session
from app.services import support as support_service


@celery_app.task(name="app.tasks.support_tickets.auto_confirm_resolved_tickets")
def auto_confirm_resolved_tickets() -> dict[str, int]:
    with task_session() as db:
        count = support_service.tickets.auto_confirm_pending(db)
        return {"auto_confirmed": count}
