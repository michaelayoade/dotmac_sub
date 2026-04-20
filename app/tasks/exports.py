"""Celery tasks for scheduled system exports."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services import web_system_export_tool as export_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.exports.run_scheduled_export")
def run_scheduled_export(**kwargs):
    with db_session_adapter.session() as session:
        return export_service.execute_scheduled_export(session, **kwargs)


@celery_app.task(name="app.tasks.exports.run_export_job")
def run_export_job(*, job_id: str):
    with db_session_adapter.session() as session:
        return export_service.process_export_job(session, job_id=job_id)
