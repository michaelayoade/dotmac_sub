"""Celery tasks for OLT CLI output capture.

These tasks capture CLI samples from OLTs for parser testing and validation.
"""

from __future__ import annotations

import logging
from uuid import UUID

from app.celery_app import celery_app
from app.db import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.olt_capture.capture_olt_samples_task",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def capture_olt_samples_task(self, olt_id: str, force: bool = False) -> dict:
    """Capture CLI output samples from an OLT.

    This task is typically triggered:
    - After a new OLT is added and connection test passes
    - After OLT firmware is updated
    - Manually for debugging

    Args:
        olt_id: UUID string of the OLT.
        force: If True, capture even if recent samples exist.

    Returns:
        Dict with capture results.
    """
    from app.services.network.olt_cli_capture import capture_olt_samples

    session = SessionLocal()
    try:
        success, message, metadata = capture_olt_samples(
            session,
            UUID(olt_id),
            force=force,
        )
        result = {
            "success": success,
            "message": message,
            "olt_id": olt_id,
        }
        if metadata:
            result["commands_captured"] = metadata.commands_captured
            result["commands_failed"] = metadata.commands_failed
            result["firmware_version"] = metadata.firmware_version

        return result

    except Exception as exc:
        logger.error("Error capturing OLT samples for %s: %s", olt_id, exc)
        raise self.retry(exc=exc)
    finally:
        session.close()


@celery_app.task(name="app.tasks.olt_capture.validate_all_parsers_task")
def validate_all_parsers_task() -> dict:
    """Validate TextFSM parsers against all captured samples.

    This can be run periodically or after template changes to detect
    parsing regressions.

    Returns:
        Dict with validation results by command.
    """
    from app.services.network.olt_cli_capture import validate_parsers_against_samples

    results = validate_parsers_against_samples()

    # Summarize results
    summary = {}
    for cmd_key, validations in results.items():
        total = len(validations)
        passed = sum(1 for v in validations if v.get("success") and v.get("row_count", 0) > 0)
        warnings = sum(1 for v in validations if v.get("warnings"))

        summary[cmd_key] = {
            "total_samples": total,
            "passed": passed,
            "with_warnings": warnings,
            "failed": total - passed,
        }

    return {
        "summary": summary,
        "details": results,
    }


@celery_app.task(name="app.tasks.olt_capture.capture_all_olts_task")
def capture_all_olts_task(force: bool = False) -> dict:
    """Capture CLI samples from all active OLTs.

    Useful for building initial corpus or refreshing after updates.

    Args:
        force: If True, capture even if recent samples exist.

    Returns:
        Dict with capture results.
    """
    from sqlalchemy import select

    from app.models.network import OLTDevice

    session = SessionLocal()
    try:
        stmt = select(OLTDevice).where(OLTDevice.is_active == True)  # noqa: E712
        olts = session.scalars(stmt).all()

        results = []
        for olt in olts:
            # Queue individual capture task
            task = capture_olt_samples_task.delay(str(olt.id), force=force)
            results.append({
                "olt_id": str(olt.id),
                "olt_name": olt.name,
                "task_id": task.id,
            })

        return {
            "queued": len(results),
            "tasks": results,
        }
    finally:
        session.close()
