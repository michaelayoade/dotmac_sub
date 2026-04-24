"""Celery tasks for periodic aggregated OLT autofind discovery."""

from __future__ import annotations

import hashlib
import logging
from time import sleep

from sqlalchemy import select

from app.celery_app import celery_app
from app.models.network import OLTDevice
from app.services.db_session_adapter import db_session_adapter
from app.services.queue_adapter import enqueue_task

logger = logging.getLogger(__name__)

# Async rediscovery polling constants
REDISCOVERY_POLL_INTERVAL_SECONDS = 5
REDISCOVERY_MAX_ATTEMPTS = 6  # 30 seconds total


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
    lock_key = _autofind_lock_key(olt_id)
    try:
        with db_session_adapter.advisory_lock(lock_key) as (db, lock_acquired):
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
        return {
            "olt_id": olt_id,
            "created": 0,
            "updated": 0,
            "resolved": 0,
            "errors": 1,
            "error": str(e),
        }


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
    try:
        with db_session_adapter.read_session() as db:
            rows = db.execute(
                select(OLTDevice.id, OLTDevice.name).where(
                    OLTDevice.is_active.is_(True)
                )
            ).all()
            olts = [(str(row.id), row.name) for row in rows]

        if not olts:
            logger.info("No active OLTs found for autofind")
            return {"olts_dispatched": 0}

        logger.info("Dispatching parallel autofind for %d OLTs", len(olts))

        dispatched = 0
        for olt_id_str, olt_name in olts:
            dispatch = enqueue_task(
                "app.tasks.ont_autofind.autofind_single_olt",
                args=[olt_id_str],
                correlation_id=f"autofind:{olt_id_str}",
                source="discover_all_olt_autofind",
            )
            if not dispatch.queued:
                logger.warning(
                    "Failed to dispatch autofind task for OLT %s (%s): %s",
                    olt_name,
                    olt_id_str,
                    dispatch.error,
                )
                continue
            dispatched += 1
            logger.debug(
                "Dispatched autofind task for OLT %s (%s)", olt_name, olt_id_str
            )

        logger.info(
            "Parallel OLT autofind orchestrator complete: dispatched %d tasks",
            dispatched,
        )
        return {"olts_dispatched": dispatched}
    except Exception as e:
        logger.error("OLT autofind orchestrator failed: %s", e, exc_info=True)
        raise


@celery_app.task(
    name="app.tasks.ont_autofind.poll_rediscovery_and_authorize",
    bind=True,
    max_retries=0,
)
def poll_rediscovery_and_authorize(
    self,  # noqa: ANN001
    olt_id: str,
    serial_number: str,
    fsp: str,
    *,
    provision_after_auth: bool = True,
    skip_acs_bind: bool = False,
    actor: str | None = None,
    poll_interval: int = REDISCOVERY_POLL_INTERVAL_SECONDS,
    max_attempts: int = REDISCOVERY_MAX_ATTEMPTS,
) -> dict[str, object]:
    """Poll for ONT rediscovery after force-deauthorize and complete authorization.

    This task runs asynchronously after a force-reauthorize deletes an existing
    ONT registration. It polls the OLT autofind cache until the ONT reappears,
    then completes the authorization workflow.

    This replaces the blocking sleep() loop in the main authorization workflow
    (Gap 8 fix), freeing up web worker threads.

    Args:
        olt_id: OLT device UUID
        serial_number: ONT serial number to look for
        fsp: Frame/Slot/Port location
        provision_after_auth: Whether to provision after authorization
        skip_acs_bind: Whether to skip ACS binding
        actor: Actor email for audit trail
        poll_interval: Seconds between poll attempts
        max_attempts: Maximum poll attempts before giving up

    Returns:
        Dict with authorization result or error details
    """
    from app.services import web_network_ont_autofind as ont_autofind_service
    from app.services.network.olt_authorization_workflow import (
        get_autofind_candidate_by_serial,
    )

    logger.info(
        "Starting async rediscovery polling: olt_id=%s serial=%s fsp=%s",
        olt_id,
        serial_number,
        fsp,
    )

    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            sleep(poll_interval)

        try:
            with db_session_adapter.session() as db:
                olt = db.get(OLTDevice, olt_id)
                if not olt:
                    return {
                        "success": False,
                        "error": "olt_not_found",
                        "message": f"OLT {olt_id} not found",
                    }

                # Refresh autofind cache
                ok, message, _stats = ont_autofind_service.sync_olt_autofind_candidates(
                    db, olt_id
                )
                if not ok:
                    logger.warning(
                        "Rediscovery poll %d/%d: autofind refresh failed for OLT %s: %s",
                        attempt,
                        max_attempts,
                        olt.name,
                        message,
                    )
                    continue

                # Check if ONT has reappeared
                candidate = get_autofind_candidate_by_serial(
                    db, olt_id, serial_number, fsp=fsp
                )
                if candidate is not None:
                    logger.info(
                        "Rediscovery poll %d/%d: ONT %s found on %s, proceeding with authorization",
                        attempt,
                        max_attempts,
                        serial_number,
                        olt.name,
                    )
                    # ONT rediscovered - now authorize it
                    from app.services.network.olt_authorization_workflow import (
                        authorize_autofind_ont_and_provision_network_audited,
                    )

                    # Note: provision_after_auth, skip_acs_bind, actor are stored
                    # but the current function signature doesn't support them.
                    # The authorization workflow handles provisioning internally.
                    result = authorize_autofind_ont_and_provision_network_audited(
                        db,
                        olt_id=olt_id,
                        fsp=fsp,
                        serial_number=serial_number,
                        force_reauthorize=False,  # Already deleted, no need to force
                        request=None,
                    )
                    return result.to_dict()

                logger.debug(
                    "Rediscovery poll %d/%d: ONT %s not yet visible on %s",
                    attempt,
                    max_attempts,
                    serial_number,
                    olt.name,
                )

        except Exception as e:
            logger.warning(
                "Rediscovery poll %d/%d failed for ONT %s: %s",
                attempt,
                max_attempts,
                serial_number,
                e,
            )

    # Exhausted attempts
    logger.warning(
        "Rediscovery polling exhausted after %d attempts: olt_id=%s serial=%s fsp=%s",
        max_attempts,
        olt_id,
        serial_number,
        fsp,
    )
    return {
        "success": False,
        "error": "rediscovery_timeout",
        "message": f"ONT {serial_number} did not reappear in autofind after {max_attempts * poll_interval}s",
        "olt_id": olt_id,
        "serial_number": serial_number,
        "fsp": fsp,
    }


def queue_rediscovery_poll(
    olt_id: str,
    serial_number: str,
    fsp: str,
    *,
    provision_after_auth: bool = True,
    skip_acs_bind: bool = False,
    actor: str | None = None,
) -> tuple[bool, str | None]:
    """Queue an async rediscovery poll task.

    Returns:
        Tuple of (success, task_id or error message)
    """
    dispatch = enqueue_task(
        "app.tasks.ont_autofind.poll_rediscovery_and_authorize",
        args=[olt_id, serial_number, fsp],
        kwargs={
            "provision_after_auth": provision_after_auth,
            "skip_acs_bind": skip_acs_bind,
            "actor": actor,
        },
        correlation_id=f"rediscovery:{olt_id}:{serial_number}",
        source="force_reauthorize",
    )
    if dispatch.queued:
        return True, dispatch.task_id
    return False, dispatch.error
