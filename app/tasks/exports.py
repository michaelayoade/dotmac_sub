"""Celery tasks for scheduled system exports."""

from __future__ import annotations

from app.celery_app import celery_app
from app.db import SessionLocal
from app.services import web_system_export_tool as export_service


@celery_app.task(name="app.tasks.exports.run_scheduled_export")
def run_scheduled_export(**kwargs):
    session = SessionLocal()
    try:
        return export_service.execute_scheduled_export(session, **kwargs)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.exports.run_export_job")
def run_export_job(*, job_id: str):
    session = SessionLocal()
    try:
        return export_service.process_export_job(session, job_id=job_id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
