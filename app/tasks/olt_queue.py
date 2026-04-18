"""Celery tasks for processing queued OLT operations.

When an OLT's circuit breaker is open, provisioning operations are queued
for later execution. This module processes those queued operations when
circuits recover.
"""

from __future__ import annotations

import logging
from typing import Any

from app.celery_app import celery_app
from app.db import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.olt_queue.process_deferred_olt_operations")
def process_deferred_olt_operations(
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Process queued operations when circuits recover.

    Runs every 30 seconds (configurable via Celery beat) to:
    1. Get pending operations ready for execution
    2. Check if OLT circuit allows operations
    3. Execute operation
    4. Mark completed or reschedule on failure
    """
    logger.debug("Processing deferred OLT operations")
    db = SessionLocal()

    try:
        from app.models.network import OLTDevice
        from app.services.network.olt_circuit_breaker import (
            can_attempt_operation,
            get_pending_operations,
            mark_operation_completed,
            mark_operation_failed,
            record_ssh_failure,
            record_ssh_success,
        )

        operations = get_pending_operations(db, limit=limit)

        if not operations:
            return {"processed": 0, "success": 0, "failed": 0, "skipped": 0}

        results = {
            "processed": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
        }

        for op in operations:
            olt = db.get(OLTDevice, op.olt_device_id)
            if not olt:
                mark_operation_failed(
                    db, op.id, "OLT not found", reschedule_seconds=None
                )
                results["failed"] += 1
                continue

            # Check circuit state
            can_proceed, reason = can_attempt_operation(db, olt)
            if not can_proceed:
                # Still blocked, skip for now
                results["skipped"] += 1
                continue

            results["processed"] += 1

            try:
                success, error = _execute_queued_operation(db, olt, op)

                if success:
                    mark_operation_completed(db, op.id)
                    record_ssh_success(db, olt)
                    results["success"] += 1
                else:
                    mark_operation_failed(db, op.id, error or "Unknown error")
                    record_ssh_failure(db, olt, error or "Unknown error")
                    results["failed"] += 1

            except Exception as e:
                logger.error(
                    "Error executing queued operation %s: %s",
                    op.id,
                    e,
                )
                mark_operation_failed(db, op.id, str(e))
                record_ssh_failure(db, olt, str(e))
                results["failed"] += 1

        db.commit()

        if results["processed"] > 0:
            logger.info(
                "Processed %d queued operations: success=%d, failed=%d, skipped=%d",
                results["processed"],
                results["success"],
                results["failed"],
                results["skipped"],
            )

        return results

    except Exception as e:
        logger.error("Error processing queued operations: %s", e)
        db.rollback()
        raise
    finally:
        db.close()


def _execute_queued_operation(
    db,
    olt,
    operation,
) -> tuple[bool, str | None]:
    """Execute a single queued operation.

    Returns:
        Tuple of (success, error_message)
    """
    op_type = operation.operation_type
    payload = operation.payload

    try:
        if op_type == "authorize":
            return _execute_authorize(db, olt, payload)
        elif op_type == "deprovision":
            return _execute_deprovision(db, olt, payload)
        elif op_type == "service_port":
            return _execute_service_port(db, olt, payload)
        else:
            return False, f"Unknown operation type: {op_type}"

    except Exception as e:
        logger.exception("Failed to execute queued %s operation", op_type)
        return False, str(e)


def _execute_authorize(
    db,
    olt,
    payload: dict,
) -> tuple[bool, str | None]:
    """Execute a queued authorization operation."""
    from app.services.network.olt_batched_auth import (
        BatchedAuthorizationSpec,
        MgmtIpConfig,
        ServicePortSpec,
        execute_batched_authorization,
    )

    # Reconstruct spec from payload
    service_ports = [ServicePortSpec(**sp) for sp in payload.get("service_ports", [])]

    mgmt_config = None
    if payload.get("mgmt_config"):
        mgmt_config = MgmtIpConfig(**payload["mgmt_config"])

    spec = BatchedAuthorizationSpec(
        serial_number=payload["serial_number"],
        fsp=payload["fsp"],
        line_profile_id=payload["line_profile_id"],
        service_profile_id=payload["service_profile_id"],
        service_ports=service_ports,
        mgmt_config=mgmt_config,
        tr069_profile_id=payload.get("tr069_profile_id"),
        description=payload.get("description"),
    )

    result = execute_batched_authorization(olt, spec)

    if result.success:
        # Update DB with ONT-ID if needed
        if result.ont_id and payload.get("ont_unit_id"):
            from app.models.network import OntUnit

            ont = db.get(OntUnit, payload["ont_unit_id"])
            if ont:
                ont.external_id = str(result.ont_id)
                ont.authorization_status = "authorized"
        return True, None
    else:
        return False, result.error_message


def _execute_deprovision(
    db,
    olt,
    payload: dict,
) -> tuple[bool, str | None]:
    """Execute a queued deprovision operation."""
    from app.services.network.olt_ssh_service_ports import delete_service_port

    service_port_indices = payload.get("service_port_indices", [])
    errors = []

    for idx in service_port_indices:
        ok, msg = delete_service_port(olt, idx)
        if not ok:
            errors.append(f"Port {idx}: {msg}")

    if errors:
        return False, "; ".join(errors)

    return True, None


def _execute_service_port(
    db,
    olt,
    payload: dict,
) -> tuple[bool, str | None]:
    """Execute a queued service-port creation operation."""
    from app.services.network.olt_ssh_service_ports import create_single_service_port

    ok, msg, _port_index = create_single_service_port(
        olt=olt,
        fsp=payload["fsp"],
        ont_id=payload["ont_id"],
        gem_index=payload["gem_index"],
        vlan_id=payload["vlan_id"],
        user_vlan=payload.get("user_vlan"),
        tag_transform=payload.get("tag_transform", "translate"),
    )

    if ok:
        return True, None
    else:
        return False, msg


@celery_app.task(name="app.tasks.olt_queue.retry_failed_operations")
def retry_failed_operations(
    *,
    max_age_hours: int = 24,
) -> dict[str, Any]:
    """Retry failed operations that are less than max_age_hours old.

    Runs hourly to attempt recovery of recently failed operations.
    """
    logger.info("Retrying failed OLT operations")
    db = SessionLocal()

    try:
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import select

        from app.models.network import QueuedOltOperation

        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)

        stmt = select(QueuedOltOperation).where(
            QueuedOltOperation.status == "failed",
            QueuedOltOperation.created_at > cutoff,
            QueuedOltOperation.attempts < 5,
        )
        failed_ops = list(db.scalars(stmt).all())

        if not failed_ops:
            return {"rescheduled": 0}

        now = datetime.now(UTC)
        for op in failed_ops:
            op.status = "pending"
            op.scheduled_for = now

        db.commit()

        logger.info("Rescheduled %d failed operations for retry", len(failed_ops))
        return {"rescheduled": len(failed_ops)}

    except Exception as e:
        logger.error("Error retrying failed operations: %s", e)
        db.rollback()
        raise
    finally:
        db.close()
