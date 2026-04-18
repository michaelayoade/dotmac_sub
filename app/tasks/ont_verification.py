"""Celery tasks for async ONT provisioning verification.

This module implements drift detection as a background task, removing
inline verification from the hot path. ONTs are verified periodically
and drift is detected asynchronously.

Pattern: Write -> Mark pending -> Return immediately
         Background task verifies state and emits events on drift
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.celery_app import celery_app
from app.db import SessionLocal

if TYPE_CHECKING:
    from app.models.network import OLTDevice, OntUnit

logger = logging.getLogger(__name__)


# Default settings
DEFAULT_VERIFICATION_INTERVAL_SEC = 300  # 5 minutes
DEFAULT_STALENESS_MINUTES = 15
DEFAULT_BATCH_SIZE = 100


@celery_app.task(name="app.tasks.ont_verification.verify_ont_provisioning_state")
def verify_ont_provisioning_state(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    staleness_minutes: int = DEFAULT_STALENESS_MINUTES,
) -> dict[str, Any]:
    """Periodic drift detection task.

    Runs every 5 minutes (configurable via Celery beat) to:
    1. Query ONTs with verification_status=pending OR stale verified_at
    2. Batch by OLT to minimize SSH connections
    3. Read actual state, compare to expected
    4. Update verification_status, emit drift events

    Returns:
        Dict with counts: verified, drifted, failed, skipped
    """
    logger.info("Starting ONT provisioning state verification")
    db = SessionLocal()

    try:
        from sqlalchemy import or_, select

        from app.models.network import OLTDevice, OntUnit

        # Calculate staleness cutoff
        staleness_cutoff = datetime.now(UTC) - timedelta(minutes=staleness_minutes)

        # Query ONTs needing verification
        stmt = (
            select(OntUnit)
            .where(
                OntUnit.is_active.is_(True),
                OntUnit.authorization_status == "authorized",
                or_(
                    OntUnit.verification_status == "pending",
                    OntUnit.verification_status.is_(None),
                    OntUnit.last_verified_at.is_(None),
                    OntUnit.last_verified_at < staleness_cutoff,
                ),
            )
            .limit(batch_size)
        )
        onts = list(db.scalars(stmt).all())

        if not onts:
            logger.info("No ONTs require verification")
            return {"verified": 0, "drifted": 0, "failed": 0, "skipped": 0}

        # Group by OLT for efficient batching
        by_olt: dict[UUID, list[OntUnit]] = {}
        for ont in onts:
            if ont.olt_device_id:
                by_olt.setdefault(ont.olt_device_id, []).append(ont)

        results = {
            "verified": 0,
            "drifted": 0,
            "failed": 0,
            "skipped": 0,
        }

        for olt_id, olt_onts in by_olt.items():
            olt = db.get(OLTDevice, olt_id)
            if not olt or not olt.is_active:
                results["skipped"] += len(olt_onts)
                continue

            # Check circuit breaker
            from app.services.network.olt_circuit_breaker import can_attempt_operation

            can_proceed, reason = can_attempt_operation(db, olt)
            if not can_proceed:
                logger.warning(
                    "Skipping verification for OLT %s: %s",
                    olt.name,
                    reason,
                )
                results["skipped"] += len(olt_onts)
                continue

            # Verify ONTs on this OLT
            olt_results = _verify_olt_onts(db, olt, olt_onts)
            results["verified"] += olt_results["verified"]
            results["drifted"] += olt_results["drifted"]
            results["failed"] += olt_results["failed"]

        db.commit()

        logger.info(
            "ONT verification complete: verified=%d, drifted=%d, failed=%d, skipped=%d",
            results["verified"],
            results["drifted"],
            results["failed"],
            results["skipped"],
        )

        return results

    except Exception as e:
        logger.error("Error in ONT verification: %s", e)
        db.rollback()
        raise
    finally:
        db.close()


def _verify_olt_onts(
    db,
    olt: OLTDevice,
    onts: list[OntUnit],
) -> dict[str, int]:
    """Verify a batch of ONTs on a single OLT.

    Uses a single SSH connection for all ONTs on this OLT.
    """
    from app.services.network.olt_circuit_breaker import (
        record_ssh_failure,
        record_ssh_success,
    )

    results = {"verified": 0, "drifted": 0, "failed": 0}
    now = datetime.now(UTC)

    try:
        # Read actual service ports for comparison

        for ont in onts:
            try:
                drift_detected = _verify_single_ont(db, olt, ont)

                ont.last_verified_at = now

                if drift_detected:
                    ont.verification_status = "drift_detected"
                    results["drifted"] += 1
                    _emit_drift_event(db, ont)
                else:
                    ont.verification_status = "verified"
                    results["verified"] += 1

            except Exception as e:
                logger.error("Failed to verify ONT %s: %s", ont.serial_number, e)
                ont.verification_status = "failed"
                results["failed"] += 1

        record_ssh_success(db, olt)

    except Exception as e:
        logger.error("SSH error verifying ONTs on OLT %s: %s", olt.name, e)
        record_ssh_failure(db, olt, str(e))
        for ont in onts:
            ont.verification_status = "failed"
            results["failed"] += 1

    return results


def _verify_single_ont(db, olt, ont) -> bool:
    """Verify a single ONT's provisioning state.

    Compares expected state (from DB) with actual state (from OLT).

    Returns:
        True if drift detected, False if verified
    """
    from app.services.network.olt_ssh_service_ports import get_service_ports_for_ont
    from app.services.network.service_port_allocator import get_allocations_for_ont

    # Get expected allocations from DB
    allocations = get_allocations_for_ont(db, ont.id)
    expected_ports = {
        (a.vlan_id, a.gem_index): a.port_index
        for a in allocations
        if a.vlan_id is not None
    }

    if not expected_ports:
        # No allocations to verify
        return False

    # Build FSP from ONT assignment
    from sqlalchemy import select

    from app.models.network import OntAssignment

    stmt = select(OntAssignment).where(
        OntAssignment.ont_unit_id == ont.id,
        OntAssignment.active.is_(True),
    )
    assignment = db.scalars(stmt).first()

    if not assignment or not assignment.pon_port:
        # Can't verify without PON port
        return False

    # Derive FSP from PON port name (e.g., "0/1/0")
    fsp = assignment.pon_port.name
    ont_id = int(ont.external_id) if ont.external_id else None

    if ont_id is None:
        return False

    # Get actual service ports from OLT
    ok, msg, actual_ports = get_service_ports_for_ont(olt, fsp, ont_id)

    if not ok:
        logger.warning(
            "Failed to read service ports for ONT %s: %s",
            ont.serial_number,
            msg,
        )
        # Can't determine drift, treat as failure
        raise RuntimeError(f"Failed to read service ports: {msg}")

    # Compare expected vs actual
    actual_vlan_gems = {(p.vlan_id, p.gem_index) for p in actual_ports}

    for key in expected_ports:
        if key not in actual_vlan_gems:
            logger.warning(
                "Drift detected on ONT %s: missing VLAN/GEM %s",
                ont.serial_number,
                key,
            )
            return True

    return False


def _emit_drift_event(db, ont) -> None:
    """Emit an event when provisioning drift is detected."""
    from sqlalchemy import select

    # Get subscriber if available
    from app.models.network import OntAssignment
    from app.services.events.dispatcher import emit_event
    from app.services.events.types import Event, EventType

    stmt = select(OntAssignment).where(
        OntAssignment.ont_unit_id == ont.id,
        OntAssignment.active.is_(True),
    )
    assignment = db.scalars(stmt).first()

    subscriber_id = assignment.subscriber_id if assignment else None

    event = Event(
        event_type=EventType.custom,
        payload={
            "type": "ont.provisioning_drift_detected",
            "ont_id": str(ont.id),
            "serial_number": ont.serial_number,
            "olt_device_id": str(ont.olt_device_id) if ont.olt_device_id else None,
            "detected_at": datetime.now(UTC).isoformat(),
        },
        subscriber_id=subscriber_id,
    )

    try:
        emit_event(db, event)
    except Exception as e:
        logger.error("Failed to emit drift event: %s", e)


@celery_app.task(name="app.tasks.ont_verification.verify_single_ont")
def verify_single_ont(*, ont_id: str) -> dict[str, Any]:
    """Verify a single ONT's provisioning state on demand.

    Use this for manual verification or after critical operations.
    """
    logger.info("Verifying single ONT: %s", ont_id)
    db = SessionLocal()

    try:
        from app.models.network import OLTDevice, OntUnit

        ont = db.get(OntUnit, ont_id)
        if not ont:
            return {"success": False, "error": "ONT not found"}

        if not ont.olt_device_id:
            return {"success": False, "error": "ONT has no OLT assigned"}

        olt = db.get(OLTDevice, ont.olt_device_id)
        if not olt:
            return {"success": False, "error": "OLT not found"}

        try:
            drift_detected = _verify_single_ont(db, olt, ont)
            ont.last_verified_at = datetime.now(UTC)

            if drift_detected:
                ont.verification_status = "drift_detected"
                _emit_drift_event(db, ont)
                db.commit()
                return {"success": True, "status": "drift_detected"}
            else:
                ont.verification_status = "verified"
                db.commit()
                return {"success": True, "status": "verified"}

        except Exception as e:
            ont.verification_status = "failed"
            db.commit()
            return {"success": False, "error": str(e)}

    except Exception as e:
        logger.error("Error verifying ONT %s: %s", ont_id, e)
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.ont_verification.mark_pending_verification")
def mark_pending_verification(*, ont_ids: list[str]) -> dict[str, Any]:
    """Mark ONTs as pending verification after writes.

    Called after service-port creation/modification to queue verification.
    """
    logger.info("Marking %d ONTs as pending verification", len(ont_ids))
    db = SessionLocal()

    try:
        from app.models.network import OntUnit

        updated = 0
        for ont_id in ont_ids:
            ont = db.get(OntUnit, ont_id)
            if ont:
                ont.verification_status = "pending"
                ont.last_applied_at = datetime.now(UTC)
                updated += 1

        db.commit()
        return {"marked_pending": updated}

    except Exception as e:
        logger.error("Error marking ONTs pending: %s", e)
        db.rollback()
        raise
    finally:
        db.close()
