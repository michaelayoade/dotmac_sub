"""Scheduled enforcement of field position observation retention."""

from __future__ import annotations

import time

from app.celery_app import celery_app
from app.metrics import observe_job
from app.services.db_session_adapter import db_session_adapter
from app.services.field.location_tracking import (
    PrunePositionObservationsCommand,
    field_location_tracking,
)
from app.services.owner_commands import CommandContext

SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.field_location.prune_field_location_pings")
def prune_field_location_pings(older_than_hours: int | None = None) -> dict[str, int]:
    start = time.monotonic()
    status = "success"
    session = SessionLocal()
    try:
        resolved_hours = (
            older_than_hours
            if older_than_hours is not None
            else field_location_tracking.retention_hours(session)
        )
        db_session_adapter.release_read_transaction(session)
        deleted = field_location_tracking.prune_observations(
            session,
            PrunePositionObservationsCommand(
                context=CommandContext.system(
                    actor="field_location_retention",
                    scope="position-observation-retention",
                    reason="scheduled_position_observation_retention",
                ),
                older_than_hours=resolved_hours,
            ),
        )
        return {"deleted": deleted, "older_than_hours": resolved_hours}
    except Exception:
        status = "error"
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()
        observe_job("field_location_ping_prune", status, time.monotonic() - start)
