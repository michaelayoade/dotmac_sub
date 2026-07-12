"""Scheduled security maintenance tasks."""

from app.celery_app import celery_app
from app.services.credential_rotation_schedule import (
    run_scheduled_credential_rotation as run_rotation,
)


@celery_app.task(name="app.tasks.security.run_scheduled_credential_rotation")
def run_scheduled_credential_rotation() -> dict[str, object]:
    return run_rotation()
