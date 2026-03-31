"""Periodic provisioning gap detection task."""

import logging

from app.celery_app import celery_app
from app.db import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.provisioning_enforcement.run_enforcement")
def run_enforcement() -> dict[str, int]:
    """Periodic task: detect provisioning gaps fleet-wide without remediation."""
    from app.services.network.provisioning_enforcement import (
        ProvisioningEnforcement,
    )

    logger.info("Starting provisioning enforcement run")
    db = SessionLocal()
    try:
        stats = ProvisioningEnforcement.run_full_enforcement(db)
        gaps = stats.get("gaps_detected", {})
        total_gaps = sum(gaps.values())
        logger.info(
            "Provisioning enforcement complete: %d gaps detected, stats=%s",
            total_gaps,
            stats,
        )
        return {"total_gaps": total_gaps, **stats}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
