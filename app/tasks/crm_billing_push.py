"""Celery task: nightly billing snapshot push to the CRM."""

from __future__ import annotations

from app.celery_app import celery_app
from app.db import task_session
from app.services.crm_billing_push import push_billing_snapshots


@celery_app.task(name="app.tasks.crm_billing_push.push_crm_billing_snapshots")
def push_crm_billing_snapshots(limit: int | None = None) -> dict:
    with task_session() as db:
        return push_billing_snapshots(db, limit=limit)
