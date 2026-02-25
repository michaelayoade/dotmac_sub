"""Celery tasks for unified VPN management operations."""

from __future__ import annotations

from app.celery_app import celery_app
from app.db import SessionLocal
from app.services import web_vpn_management as vpn_management_service


@celery_app.task(name="app.tasks.vpn.run_vpn_control_job")
def run_vpn_control_job(*, job_id: str):
    session = SessionLocal()
    try:
        return vpn_management_service.execute_control_job(session, job_id=job_id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.vpn.run_vpn_health_scan")
def run_vpn_health_scan():
    session = SessionLocal()
    try:
        return vpn_management_service.run_health_scan(session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
