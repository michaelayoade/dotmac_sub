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
    """Auto-link provisioning profiles to ONTs without one.

    Scans active ONTs that have no profile and attempts to resolve one via
    ``resolve_profile_for_ont``. The resolver only returns a directly
    assigned profile (no owner-based default lookup), so this task is
    effectively a no-op unless an ONT already carries a profile reference.
    """
    logger.info("Starting auto-link of provisioning profiles to ONTs")
    with db_session_adapter.session() as db:
        from sqlalchemy import select

        from app.models.network import OntUnit
        from app.services.network.ont_profile_apply import resolve_profile_for_ont

        stmt = (
            select(OntUnit.id)
            .where(
                OntUnit.provisioning_profile_id.is_(None),
                OntUnit.is_active.is_(True),
            )
            .limit(500)
        )
        ont_rows = list(db.execute(stmt).all())

        linked = 0
        errors = 0
        for (ont_id,) in ont_rows:
            try:
                profile = resolve_profile_for_ont(db, str(ont_id))
                if profile:
                    ont = db.get(OntUnit, ont_id)
                    if ont:
                        ont.provisioning_profile_id = profile.id
                        linked += 1
            except Exception as e:
                logger.error("Error linking profile for ONT %s: %s", ont_id, e)
                errors += 1

        if linked:
            db.commit()
            logger.info("Auto-linked %d ONTs to provisioning profiles", linked)
        else:
            logger.info("No ONTs needed profile auto-linking")

        return {
            "linked": linked,
            "skipped": len(ont_rows) - linked - errors,
            "errors": errors,
        }
