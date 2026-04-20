"""Celery tasks for unified VPN management operations."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services import web_vpn_management as vpn_management_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.vpn.run_vpn_control_job")
def run_vpn_control_job(*, job_id: str):
    with db_session_adapter.session() as session:
        return vpn_management_service.execute_control_job(session, job_id=job_id)


@celery_app.task(name="app.tasks.vpn.run_vpn_health_scan")
def run_vpn_health_scan():
    with db_session_adapter.session() as session:
        return vpn_management_service.run_health_scan(session)
