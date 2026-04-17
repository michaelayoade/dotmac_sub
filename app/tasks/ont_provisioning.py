"""Celery tasks for ONT provisioning profile management."""

import logging

from app.celery_app import celery_app
from app.db import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ont_provisioning.detect_profile_drift")
def detect_profile_drift() -> dict[str, int]:
    """Scan all profile-linked ONTs for configuration drift.

    Compares each ONT's current flat-field config against its assigned
    OntProvisioningProfile desired state. Marks drifted ONTs with
    provisioning_status=drift_detected.
    """
    logger.info("Starting ONT provisioning profile drift detection")
    db = SessionLocal()
    try:
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
    except Exception as e:
        logger.error("Error in drift detection: %s", e)
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.ont_provisioning.auto_link_profiles")
def auto_link_profiles() -> dict[str, int]:
    """Auto-link provisioning profiles to ONTs without one.

    For ONTs that have an active subscription but no provisioning_profile_id,
    attempt to resolve a profile from the subscription's offer default or
    organization default, and assign it.
    """
    logger.info("Starting auto-link of provisioning profiles to ONTs")
    db = SessionLocal()
    try:
        from sqlalchemy import select

        from app.models.network import OntAssignment, OntUnit
        from app.models.subscriber import Subscriber
        from app.services.network.ont_profile_apply import resolve_profile_for_ont

        # Find ONTs with active assignments (to subscribers) but no profile.
        # We also select the assignment's subscriber_id so we can pass the
        # business/subscriber context through to the network-layer resolver
        # (which intentionally does not import from the subscriber domain).
        stmt = (
            select(OntUnit.id, OntAssignment.subscriber_id)
            .join(OntAssignment, OntAssignment.ont_unit_id == OntUnit.id)
            .where(
                OntUnit.provisioning_profile_id.is_(None),
                OntUnit.is_active.is_(True),
                OntAssignment.active.is_(True),
                OntAssignment.subscriber_id.isnot(None),
            )
            .limit(500)
        )
        ont_rows = list(db.execute(stmt).all())

        linked = 0
        errors = 0
        for ont_id, subscriber_id in ont_rows:
            try:
                owner_is_business: bool | None = None
                if subscriber_id is not None:
                    subscriber = db.get(Subscriber, subscriber_id)
                    owner_is_business = bool(
                        subscriber and getattr(subscriber, "is_business", False)
                    )
                profile = resolve_profile_for_ont(
                    db,
                    str(ont_id),
                    owner_subscriber_id=subscriber_id,
                    owner_is_business=owner_is_business,
                )
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
    except Exception as e:
        logger.error("Error in auto-link profiles: %s", e)
        db.rollback()
        raise
    finally:
        db.close()
