"""Scheduled adapter publishing the composed-module outbox backlog (SUB-2)."""

from __future__ import annotations

import logging

from sqlalchemy.exc import OperationalError

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.observability import (
    StateObservation,
    publish_state_snapshot,
    record_task_run,
)
from app.services.outbox_backlog import read_outbox_backlog

logger = logging.getLogger(__name__)

TASK_NAME = "app.tasks.outbox_backlog.publish_outbox_backlog"


@celery_app.task(
    name=TASK_NAME,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def publish_outbox_backlog_task() -> dict[str, float]:
    try:
        with db_session_adapter.read_session() as db:
            backlog = read_outbox_backlog(db)
    except Exception:
        publish_state_snapshot(
            "outbox_backlog",
            (StateObservation(signal="failure", scope="global", value=1),),
            status="error",
        )
        record_task_run(TASK_NAME, status="error", counters={"failure": 1})
        logger.exception("outbox_backlog_read_failed")
        raise
    observations = {
        "pending": float(backlog.pending),
        "claimed": float(backlog.claimed),
        "dead": float(backlog.dead),
        "oldest_pending_age_seconds": backlog.oldest_pending_age_seconds,
    }
    publish_state_snapshot(
        "outbox_backlog",
        tuple(
            StateObservation(signal=signal, scope="global", value=value)
            for signal, value in observations.items()
        ),
    )
    record_task_run(
        TASK_NAME,
        status="success",
        counters={"pending": backlog.pending, "dead": backlog.dead},
    )
    return observations
