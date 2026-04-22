"""Celery tasks for ONT provisioning profile management."""

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ont_provisioning.detect_profile_drift")
def detect_profile_drift() -> dict[str, int]:
    """Scan all profile-linked ONTs for configuration drift.

    Compares each ONT's current flat-field config against its assigned
    OntProvisioningProfile desired state. Marks drifted ONTs with
    provisioning_status=drift_detected.
    """
    logger.info("Starting ONT provisioning profile drift detection")
    with db_session_adapter.session() as db:
        from app.services.network.ont_profile_apply import detect_drift_batch

        reports = detect_drift_batch(db, limit=1000)
        drifted = len(reports)
        total_fields = sum(len(r.drifted_fields) for r in reports)

        if drifted:
            logger.warning(
                "Drift detected on %d ONTs (%d total field mismatches)",
                drifted,
                total_fields,
            )
        else:
            logger.info("No drift detected across profiled ONTs")

        return {"drifted": drifted, "total_field_mismatches": total_fields, "errors": 0}


@celery_app.task(name="app.tasks.ont_provisioning.auto_link_profiles")
def auto_link_profiles() -> dict[str, int]:
    """Legacy compatibility task retained as a no-op after bundle cutover."""
    logger.info(
        "Skipping legacy ONT auto-link task because active bundle assignments are now authoritative"
    )
    return {"linked": 0, "skipped": 0, "errors": 0}
