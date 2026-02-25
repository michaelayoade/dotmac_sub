"""Celery tasks for provisioning workflows."""

from __future__ import annotations

from app.celery_app import celery_app
from app.db import SessionLocal
from app.services import web_provisioning_bulk_activate as bulk_activate_service


@celery_app.task(name="app.tasks.provisioning.run_bulk_activation_job")
def run_bulk_activation_job(*, job_id: str):
    session = SessionLocal()
    try:
        return bulk_activate_service.execute_job(session, job_id=job_id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
