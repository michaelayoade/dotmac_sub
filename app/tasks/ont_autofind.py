"""Celery tasks for periodic aggregated OLT autofind discovery."""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select, text

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.network import OLTDevice

logger = logging.getLogger(__name__)


def _autofind_lock_key(olt_id: str) -> int:
    """Generate a unique advisory lock key for autofind on an OLT.

    Uses a dedicated namespace (7044) to avoid collisions with other locks.
    PostgreSQL advisory locks accept bigint (-2^63 to 2^63-1).
    """
    hash_bytes = hashlib.sha256(olt_id.encode()).digest()[:8]
    hash_int = int.from_bytes(hash_bytes, byteorder="big", signed=True)
    namespace = 7044 << 48
    return namespace | (hash_int & 0x0000FFFFFFFFFFFF)


@celery_app.task(name="app.tasks.ont_autofind.autofind_single_olt")
def autofind_single_olt(olt_id: str) -> dict[str, int | str]:
    """Scan a single OLT for unconfigured ONTs and cache the results.

    This task is designed to run in parallel with other autofind tasks.
    Each task handles its own database session and transaction.
    Uses per-OLT advisory lock to prevent concurrent autofind on the same device.

    Args:
        olt_id: UUID string of the OLT to scan.

    Returns:
        Stats dict with olt_name, created, updated, resolved, errors.
    """
    from app.services import web_network_ont_autofind as ont_autofind_service

    logger.info("Starting single OLT autofind for %s", olt_id)
    db = SessionLocal()
    lock_key = _autofind_lock_key(olt_id)
    lock_acquired = False
    try:
        lock_acquired = bool(
            db.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": lock_key},
            ).scalar()
        )
        if not lock_acquired:
            logger.warning(
                "Skipping autofind for OLT %s: another autofind already in progress",
                olt_id,
            )
            return {
                "olt_id": olt_id,
                "created": 0,
                "updated": 0,
                "resolved": 0,
                "errors": 0,
                "skipped_due_to_lock": 1,
            }

        olt = db.get(OLTDevice, olt_id)
        if not olt:
            logger.warning("Autofind: OLT %s not found", olt_id)
            return {
                "olt_id": olt_id,
                "created": 0,
                "updated": 0,
                "resolved": 0,
                "errors": 1,
                "error": "olt_not_found",
            }

        ok, message, stats = ont_autofind_service.sync_olt_autofind_candidates(
            db, olt_id
        )

        if ok:
            logger.info(
                "Autofind complete for OLT %s (%s): %s",
                olt.name,
                olt.mgmt_ip,
                stats,
            )
            return {
                "olt_id": olt_id,
                "olt_name": olt.name,
                "created": int(stats.get("created", 0)),
                "updated": int(stats.get("updated", 0)),
                "resolved": int(stats.get("resolved", 0)),
                "errors": 0,
            }
        else:
            logger.warning(
                "Autofind failed for OLT %s (%s): %s",
                olt.name,
                olt.mgmt_ip,
                message,
            )
            return {
                "olt_id": olt_id,
                "olt_name": olt.name,
                "created": 0,
                "updated": 0,
                "resolved": 0,
                "errors": 1,
                "error": message,
            }
    except Exception as e:
        logger.error("Autofind failed for OLT %s: %s", olt_id, e, exc_info=True)
        db.rollback()
        return {
            "olt_id": olt_id,
            "created": 0,
            "updated": 0,
            "resolved": 0,
            "errors": 1,
            "error": str(e),
        }
    finally:
        if lock_acquired:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": lock_key},
                )
            except Exception:
                logger.exception("Failed to release autofind lock for OLT %s", olt_id)
        db.close()


@celery_app.task(name="app.tasks.ont_autofind.discover_all_olt_autofind")
def discover_all_olt_autofind() -> dict[str, int]:
    """Periodic task to scan all active OLTs for unconfigured ONTs.

    Fans out to parallel autofind_single_olt tasks for each active OLT.
    Each subtask runs independently with its own per-OLT advisory lock,
    preventing concurrent autofind on the same device even if this
    orchestrator is triggered multiple times.

    Returns:
        Statistics dict with olts_dispatched count.
    """
    logger.info("Starting parallel OLT autofind orchestrator")
    db = SessionLocal()
    try:
        olts = list(
            db.scalars(select(OLTDevice).where(OLTDevice.is_active.is_(True))).all()
        )

        if not olts:
            logger.info("No active OLTs found for autofind")
            return {"olts_dispatched": 0}

        logger.info("Dispatching parallel autofind for %d OLTs", len(olts))

        from app.celery_app import enqueue_celery_task

        dispatched = 0
        for olt in olts:
            enqueue_celery_task(
                autofind_single_olt,
                args=[str(olt.id)],
                correlation_id=f"autofind:{olt.id}",
                source="discover_all_olt_autofind",
            )
            dispatched += 1
            logger.debug("Dispatched autofind task for OLT %s (%s)", olt.name, olt.id)

        logger.info(
            "Parallel OLT autofind orchestrator complete: dispatched %d tasks",
            dispatched,
        )
        return {"olts_dispatched": dispatched}
    except Exception as e:
        logger.error("OLT autofind orchestrator failed: %s", e, exc_info=True)
        db.rollback()
        raise
    finally:
        db.close()
