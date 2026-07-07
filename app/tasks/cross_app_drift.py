"""Celery task for the cross-app drift detector (read-only / detect-only)."""

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.cross_app_drift.run_cross_app_drift_detection")
def run_cross_app_drift_detection() -> dict:
    """Run every drift check, persist findings by fingerprint, and WARN on
    material (critical/high) open drift. Detect-only — it never heals; each
    finding names the reconciler that should. Returns run counts."""
    db = db_session_adapter.create_session()
    try:
        from app.services import cross_app_drift

        run = cross_app_drift.run_detection(db)
        by_severity = cross_app_drift.open_findings_by_severity(db)
        # Mirror material findings into the admin alert console (the alert path).
        alerts = cross_app_drift.sync_drift_alerts(db)
        material = by_severity.get("critical", 0) + by_severity.get("high", 0)
        if material:
            logger.warning(
                "cross_app_drift: %s material finding(s) open "
                "(critical=%s high=%s medium=%s low=%s); new=%s resolved=%s",
                material,
                by_severity.get("critical", 0),
                by_severity.get("high", 0),
                by_severity.get("medium", 0),
                by_severity.get("low", 0),
                run.findings_new,
                run.findings_resolved,
            )
        else:
            logger.info(
                "cross_app_drift: no material drift; open=%s new=%s resolved=%s",
                run.findings_open,
                run.findings_new,
                run.findings_resolved,
            )
        return {
            "checks_run": run.checks_run,
            "findings_new": run.findings_new,
            "findings_resolved": run.findings_resolved,
            "findings_open": run.findings_open,
            "open_by_severity": by_severity,
            "alerts": alerts,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
