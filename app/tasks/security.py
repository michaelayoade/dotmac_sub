"""Scheduled security maintenance tasks."""

import logging
import time

from app.celery_app import celery_app
from app.services.credential_rotation_schedule import (
    run_scheduled_credential_rotation as run_rotation,
)
from app.services.observability import (
    publish_state_snapshot,
    record_task_run,
    record_task_skip,
)

logger = logging.getLogger(__name__)

_TASK_NAME = "app.tasks.security.run_scheduled_credential_rotation"


@celery_app.task(name=_TASK_NAME)
def run_scheduled_credential_rotation() -> dict[str, object]:
    started = time.monotonic()
    try:
        result = run_rotation()
    except Exception:
        logger.exception("credential_rotation_task_failed")
        try:
            publish_state_snapshot("credentials", [], status="error")
        except Exception:
            logger.debug("credential_error_snapshot_failed", exc_info=True)
        record_task_run(
            _TASK_NAME,
            status="error",
            counters={},
            duration_seconds=time.monotonic() - started,
        )
        raise

    if result.get("status") == "already_running":
        record_task_skip(_TASK_NAME, reason="already_running")
        return result

    task_status = "error" if result.get("status") == "blocked" else "success"
    record_task_run(
        _TASK_NAME,
        status=task_status,
        counters=result,
        duration_seconds=time.monotonic() - started,
    )
    return result
