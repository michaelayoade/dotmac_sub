"""Scheduled per-ONT status + Rx snapshot (splice-inference substrate).

Appends one ``OntSignalObservation`` row per active ONT each sweep, freezing the
live ``ont_units.olt_status`` / ``onu_rx_signal_dbm`` columns into a time series.
Splice inference (``app/services/topology/splice_inference.py``, design §6) reads
that history to recover the unpollable sub-PON splitter branches: ONTs that go
dark together (co-failure) or droop by the same dB (correlated Rx) share a branch.

Routed to the ``ingestion`` queue like the other topology sweeps. Read-only
against the OLTs (it only reads columns other pollers already populate);
single-flight via a Postgres advisory lock; commits the appended rows on success.
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select, text

from app.celery_app import celery_app
from app.models.network import OntSignalObservation, OntUnit
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

# Stable advisory-lock key (single-flight across workers/beats).
_OBS_LOCK_KEY = 70420615


@celery_app.task(
    name="app.tasks.ont_signal_observations.record_ont_observations",
    soft_time_limit=300,
    time_limit=360,
)
def record_ont_observations() -> dict[str, Any]:
    """Snapshot every active ONT's status + Rx into ont_signal_observations."""
    db = db_session_adapter.create_session()
    try:
        lock_acquired = bool(
            db.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": _OBS_LOCK_KEY},
            ).scalar()
        )
        if not lock_acquired:
            logger.warning(
                "ont_signal_observations_skip_locked: previous run in progress."
            )
            return {"skipped_due_to_lock": 1}
        try:
            onts = db.execute(
                select(
                    OntUnit.id,
                    OntUnit.olt_device_id,
                    OntUnit.pon_port_id,
                    OntUnit.olt_status,
                    OntUnit.onu_rx_signal_dbm,
                ).where(OntUnit.is_active.is_(True))
            ).all()
            db.add_all(
                OntSignalObservation(
                    ont_unit_id=row.id,
                    olt_device_id=row.olt_device_id,
                    pon_port_id=row.pon_port_id,
                    olt_status=row.olt_status,
                    rx_signal_dbm=row.onu_rx_signal_dbm,
                )
                for row in onts
            )
            db.commit()
            logger.info("ont_signal_observations_done recorded=%d", len(onts))
            return {"recorded": len(onts)}
        except SoftTimeLimitExceeded:
            db.rollback()
            logger.warning("ont_signal_observations_timed_out")
            return {"error": "ont_signal_observations_timed_out"}
        except Exception as exc:  # noqa: BLE001 - report and roll back
            db.rollback()
            logger.exception("ont_signal_observations_failed")
            return {"error": str(exc)}
        finally:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _OBS_LOCK_KEY},
                )
            except Exception:
                logger.exception("ont_signal_observations_unlock_failed")
    finally:
        db.close()
