"""Celery tasks for periodic ONT discovery via SNMP on all active OLTs."""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select

from app.celery_app import celery_app
from app.models.network import OLTDevice
from app.services.db_session_adapter import db_session_adapter
from app.services.queue_adapter import enqueue_task

logger = logging.getLogger(__name__)


def _ont_discovery_lock_key(olt_id: str) -> int:
    """Generate a unique advisory lock key for ONT discovery on an OLT.

    Uses a dedicated namespace (7043) to avoid collisions with OLT polling locks.
    PostgreSQL advisory locks accept bigint (-2^63 to 2^63-1).
    """
    hash_bytes = hashlib.sha256(olt_id.encode()).digest()[:8]
    hash_int = int.from_bytes(hash_bytes, byteorder="big", signed=True)
    namespace = 7043 << 48
    return namespace | (hash_int & 0x0000FFFFFFFFFFFF)


@celery_app.task(name="app.tasks.ont_discovery.discover_single_olt_onts")
def discover_single_olt_onts(olt_id: str) -> dict[str, int | str]:
    """Discover ONTs from a single OLT via SNMP and upsert OntUnit rows.

    This task is designed to run in parallel with other ONT discovery tasks.
    Each task handles its own database session and transaction.
    Uses per-OLT advisory lock to prevent concurrent discovery on the same device.

    Args:
        olt_id: UUID string of the OLT to discover ONTs from.

    Returns:
        Stats dict with olt_name, created, updated, errors.
    """
    logger.info("Starting single OLT ONT discovery for %s", olt_id)
    lock_key = _ont_discovery_lock_key(olt_id)
    try:
        with db_session_adapter.advisory_lock(lock_key) as (db, lock_acquired):
            if not lock_acquired:
                logger.warning(
                    "Skipping ONT discovery for OLT %s: another discovery already in progress",
                    olt_id,
                )
                return {
                    "olt_id": olt_id,
                    "created": 0,
                    "updated": 0,
                    "errors": 0,
                    "skipped_due_to_lock": 1,
                }

            from app.services.network.olt_snmp_sync import (
                sync_onts_from_olt_snmp_tracked,
            )

            olt = db.get(OLTDevice, olt_id)
            if not olt:
                logger.warning("ONT discovery: OLT %s not found", olt_id)
                return {
                    "olt_id": olt_id,
                    "created": 0,
                    "updated": 0,
                    "errors": 1,
                    "error": "olt_not_found",
                }

            ok, msg, stats = sync_onts_from_olt_snmp_tracked(
                db, olt_id, initiated_by="celery:discover_single_olt_onts"
            )

            if ok:
                created = (
                    int(str(stats.get("created", 0))) if stats.get("created") else 0
                )
                updated = (
                    int(str(stats.get("updated", 0))) if stats.get("updated") else 0
                )
                logger.info(
                    "ONT discovery complete for OLT %s (%s): created=%d, updated=%d",
                    olt.name,
                    olt.mgmt_ip,
                    created,
                    updated,
                )
                return {
                    "olt_id": olt_id,
                    "olt_name": olt.name,
                    "created": created,
                    "updated": updated,
                    "errors": 0,
                }
            logger.warning(
                "ONT discovery skipped for OLT %s (%s): %s",
                olt.name,
                olt.mgmt_ip,
                msg,
            )
            return {
                "olt_id": olt_id,
                "olt_name": olt.name,
                "created": 0,
                "updated": 0,
                "skipped": 1,
                "skip_reason": msg,
                "errors": 0,
            }
    except Exception as e:
        logger.error("ONT discovery failed for OLT %s: %s", olt_id, e, exc_info=True)
        return {
            "olt_id": olt_id,
            "created": 0,
            "updated": 0,
            "errors": 1,
            "error": str(e),
        }

@celery_app.task(name="app.tasks.ont_discovery.discover_all_olt_onts")
def discover_all_olt_onts() -> dict[str, int]:
    """Periodic task to discover ONTs from all active OLTs.

    Fans out to parallel discover_single_olt_onts tasks for each active OLT.
    Each subtask runs independently with its own per-OLT advisory lock,
    preventing concurrent discovery on the same device even if this
    orchestrator is triggered multiple times.

    Returns:
        Statistics dict with olts_dispatched count.
    """
    logger.info("Starting parallel ONT discovery orchestrator")
    try:
        with db_session_adapter.read_session() as db:
            stmt = select(OLTDevice.id, OLTDevice.name).where(
                OLTDevice.is_active.is_(True)
            )
            olts = [(str(row.id), row.name) for row in db.execute(stmt).all()]

        if not olts:
            logger.info("No active OLTs found for ONT discovery")
            return {"olts_dispatched": 0}

        logger.info("Dispatching parallel ONT discovery for %d OLTs", len(olts))

        dispatched = 0
        for olt_id_str, olt_name in olts:
            dispatch = enqueue_task(
                "app.tasks.ont_discovery.discover_single_olt_onts",
                args=[olt_id_str],
                correlation_id=f"ont_discovery:{olt_id_str}",
                source="discover_all_olt_onts",
            )
            if not dispatch.queued:
                logger.warning(
                    "Failed to dispatch ONT discovery task for OLT %s (%s): %s",
                    olt_name,
                    olt_id_str,
                    dispatch.error,
                )
                continue
            dispatched += 1
            logger.debug(
                "Dispatched ONT discovery task for OLT %s (%s)",
                olt_name,
                olt_id_str,
            )

        logger.info(
            "Parallel ONT discovery orchestrator complete: dispatched %d tasks",
            dispatched,
        )
        return {"olts_dispatched": dispatched}
    except Exception as e:
        logger.error("ONT discovery orchestrator failed: %s", e, exc_info=True)
        raise
