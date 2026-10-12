"""Celery adapters for the router health reconciler.

Adapters only: open a session, call one reconciler function, close. Every task
takes a Postgres advisory lock so overlapping runs skip, honours its control
again at run time, and never commits (owner commands do).
"""

from __future__ import annotations

import logging
import time
import zlib
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.services import control_registry, observability
from app.services.db_session_adapter import db_session_adapter
from app.services.router_health import coa_canary, reconciler
from app.tasks._postgres_lock import postgres_session_advisory_lock

logger = logging.getLogger(__name__)

TASK_POSTURE = "app.tasks.router_health.reconcile_router_posture"
TASK_SERVICE = "app.tasks.router_health.reconcile_service_path"
TASK_CANARY = "app.tasks.router_health.run_coa_canary"
TASK_REPROBE = "app.tasks.router_health.reprobe_coa_canary"
CANARY_SOFT_LIMIT_SECONDS = 15 * 60
_LOCK_TIMEOUT_MS = 2000


def _lock_key(task_name: str) -> int:
    """Stable positive advisory-lock key for one router-health task."""
    return zlib.crc32(f"router_health:{task_name}".encode()) & 0x7FFFFFFF


SOFT_LIMIT_SECONDS = {
    TASK_POSTURE: 4 * 3600,
    TASK_SERVICE: 40 * 60,
}


def _run(
    task_name: str, control_key: str, body: Callable[[Session], dict[str, Any]]
) -> dict[str, Any]:
    started = time.monotonic()
    db = db_session_adapter.create_session()
    try:
        if not control_registry.is_enabled(db, control_key):
            db.rollback()
            return {"skipped": "control_disabled"}
        with postgres_session_advisory_lock(
            _lock_key(task_name), timeout_ms=_LOCK_TIMEOUT_MS
        ) as acquired:
            if not acquired:
                observability.record_task_skip(task_name, reason="lock_held")
                return {"skipped": "lock_held"}
            counters = body(db)
        observability.record_task_run(
            task_name,
            status="ok",
            counters={k: int(v) for k, v in counters.items() if isinstance(v, int)},
            duration_seconds=time.monotonic() - started,
        )
        return counters
    finally:
        db.close()


@celery_app.task(name=TASK_POSTURE, soft_time_limit=SOFT_LIMIT_SECONDS[TASK_POSTURE])
def reconcile_router_posture() -> dict[str, Any]:
    def body(db: Session) -> dict[str, Any]:
        stats = reconciler.reconcile_router_posture(db, task_name=TASK_POSTURE)
        return {
            "routers": stats.subjects,
            "findings": stats.findings,
            "opened": stats.opened,
            "resolved": stats.resolved,
            "capped": stats.capped,
            "unparsed": stats.unparsed,
        }

    return _run(TASK_POSTURE, reconciler.CONTROL_POSTURE, body)


@celery_app.task(name=TASK_SERVICE, soft_time_limit=SOFT_LIMIT_SECONDS[TASK_SERVICE])
def reconcile_service_path() -> dict[str, Any]:
    def body(db: Session) -> dict[str, Any]:
        stats = reconciler.reconcile_service_path(db, task_name=TASK_SERVICE)
        return {
            "nas": stats.subjects,
            "findings": stats.findings,
            "opened": stats.opened,
            "resolved": stats.resolved,
        }

    return _run(TASK_SERVICE, reconciler.CONTROL_POSTURE, body)


def _schedule_reprobe(nas_device_id: UUID, run_id: UUID, countdown: int) -> None:
    reprobe_coa_canary.apply_async(
        kwargs={"nas_device_id": str(nas_device_id), "run_id": str(run_id)},
        countdown=countdown,
    )


@celery_app.task(name=TASK_CANARY, soft_time_limit=CANARY_SOFT_LIMIT_SECONDS)
def run_coa_canary() -> dict[str, Any]:
    def body(db: Session) -> dict[str, Any]:
        stats = coa_canary.run_coa_canary(
            db, task_name=TASK_CANARY, schedule_reprobe=_schedule_reprobe
        )
        return {
            "targets": stats.targets,
            "healthy": stats.healthy,
            "failed_pending_reprobe": stats.failed_pending_reprobe,
            "findings": stats.findings,
            "skipped": stats.skipped or "",
        }

    return _run(TASK_CANARY, reconciler.CONTROL_CANARY, body)


@celery_app.task(name=TASK_REPROBE, soft_time_limit=CANARY_SOFT_LIMIT_SECONDS)
def reprobe_coa_canary(nas_device_id: str, run_id: str) -> dict[str, Any]:
    def body(db: Session) -> dict[str, Any]:
        stats = coa_canary.reprobe_coa_canary(
            db,
            nas_device_id=UUID(nas_device_id),
            run_id=UUID(run_id),
            task_name=TASK_REPROBE,
        )
        return {
            "healthy": stats.healthy,
            "findings": stats.findings,
            "skipped": stats.skipped or "",
        }

    return _run(TASK_REPROBE, reconciler.CONTROL_CANARY, body)
