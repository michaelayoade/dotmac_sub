"""Celery tasks for TR-069 background operations.

Handles periodic device sync from GenieACS, queued job execution with retry,
device health monitoring, and session/job retention cleanup.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.tr069 import (
    Tr069AcsServer,
    Tr069CpeDevice,
    Tr069Job,
    Tr069JobStatus,
    Tr069Session,
)

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.tr069.sync_all_acs_devices")
def sync_all_acs_devices() -> dict[str, int]:
    """Periodic sync of devices from all active ACS servers.

    Iterates over active Tr069AcsServer records and calls
    CpeDevices.sync_from_genieacs() for each.

    Returns:
        Stats: {servers_synced, total_created, total_updated, errors}.
    """
    logger.info("Starting TR-069 ACS device sync")
    db = SessionLocal()
    try:
        servers = list(
            db.scalars(
                select(Tr069AcsServer).where(Tr069AcsServer.is_active.is_(True))
            ).all()
        )
        if not servers:
            logger.info("No active ACS servers to sync")
            return {
                "servers_synced": 0,
                "total_created": 0,
                "total_updated": 0,
                "errors": 0,
            }

        from app.services.tr069 import CpeDevices

        synced = 0
        total_created = 0
        total_updated = 0
        errors = 0

        for server in servers:
            try:
                result = CpeDevices.sync_from_genieacs(db, str(server.id))
                total_created += result.get("created", 0)
                total_updated += result.get("updated", 0)
                synced += 1
            except Exception as e:
                logger.error(
                    "Failed to sync ACS server %s (%s): %s", server.name, server.id, e
                )
                errors += 1

        # Emit event for newly discovered devices
        if total_created > 0:
            try:
                from app.services.events import emit_event
                from app.services.events.types import EventType

                emit_event(
                    db,
                    EventType.tr069_device_discovered,
                    {
                        "servers_synced": synced,
                        "created": total_created,
                        "updated": total_updated,
                    },
                    actor="system",
                )
            except Exception as e:
                logger.warning("Failed to emit tr069_device_discovered event: %s", e)

        logger.info(
            "TR-069 sync complete: %d servers, %d created, %d updated, %d errors",
            synced,
            total_created,
            total_updated,
            errors,
        )
        return {
            "servers_synced": synced,
            "total_created": total_created,
            "total_updated": total_updated,
            "errors": errors,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.tr069.execute_pending_jobs")
def execute_pending_jobs() -> dict[str, int]:
    """Execute queued TR-069 jobs and retry failed jobs with backoff.

    Picks up jobs in 'queued' status and executes them via GenieACS.
    Also retries 'failed' jobs that haven't exceeded max_retries, using
    exponential backoff (1m, 5m, 15m).

    Returns:
        Stats: {executed, succeeded, failed, retried, skipped}.
    """
    logger.info("Starting TR-069 job execution")
    db = SessionLocal()
    try:
        from app.services.events import emit_event
        from app.services.events.types import EventType
        from app.services.tr069 import Jobs

        now = datetime.now(UTC)
        executed = 0
        succeeded = 0
        failed = 0
        retried = 0
        skipped = 0

        # 1. Execute queued jobs
        queued_jobs = list(
            db.scalars(
                select(Tr069Job)
                .where(Tr069Job.status == Tr069JobStatus.queued)
                .order_by(Tr069Job.created_at.asc())
                .limit(50)
            ).all()
        )
        for job in queued_jobs:
            try:
                result = Jobs.execute(db, str(job.id))
                executed += 1
                if result.status == Tr069JobStatus.succeeded:
                    succeeded += 1
                    _emit_job_event(
                        db, emit_event, EventType.tr069_job_completed, result
                    )
                else:
                    failed += 1
                    _emit_job_event(db, emit_event, EventType.tr069_job_failed, result)
            except Exception as e:
                logger.error("Failed to execute job %s: %s", job.id, e)
                failed += 1

        # 2. Retry failed jobs with exponential backoff
        backoff_minutes = [1, 5, 15]
        failed_jobs = list(
            db.scalars(
                select(Tr069Job)
                .where(
                    Tr069Job.status == Tr069JobStatus.failed,
                    Tr069Job.retry_count < Tr069Job.max_retries,
                )
                .order_by(Tr069Job.completed_at.asc())
                .limit(20)
            ).all()
        )
        for job in failed_jobs:
            backoff_idx = min(job.retry_count, len(backoff_minutes) - 1)
            backoff = timedelta(minutes=backoff_minutes[backoff_idx])
            if job.completed_at and (now - job.completed_at) < backoff:
                skipped += 1
                continue
            try:
                job.retry_count += 1
                db.commit()
                result = Jobs.execute(db, str(job.id))
                retried += 1
                if result.status == Tr069JobStatus.succeeded:
                    succeeded += 1
                    _emit_job_event(
                        db, emit_event, EventType.tr069_job_completed, result
                    )
                else:
                    failed += 1
            except Exception as e:
                logger.error("Failed to retry job %s: %s", job.id, e)

        # 3. Cancel stale running jobs (stuck > 10 minutes)
        stale_cutoff = now - timedelta(minutes=10)
        stale_jobs = list(
            db.scalars(
                select(Tr069Job).where(
                    Tr069Job.status == Tr069JobStatus.running,
                    Tr069Job.started_at < stale_cutoff,
                )
            ).all()
        )
        for job in stale_jobs:
            job.status = Tr069JobStatus.failed
            job.error = "Timed out after 10 minutes"
            job.completed_at = now
            logger.warning("Marked stale TR-069 job %s as failed (timeout)", job.id)
        if stale_jobs:
            db.commit()

        logger.info(
            "TR-069 job execution: %d executed, %d succeeded, %d failed, %d retried, %d skipped",
            executed,
            succeeded,
            failed,
            retried,
            skipped,
        )
        return {
            "executed": executed,
            "succeeded": succeeded,
            "failed": failed,
            "retried": retried,
            "skipped": skipped,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.tr069.check_device_health")
def check_device_health() -> dict[str, int]:
    """Check TR-069 device health by last_inform_at freshness.

    Marks devices as stale if they haven't informed within 24 hours.
    Emits tr069_device_stale events for devices newly going stale.

    Returns:
        Stats: {total_checked, healthy, stale, errors}.
    """
    logger.info("Starting TR-069 device health check")
    db = SessionLocal()
    try:
        now = datetime.now(UTC)
        stale_cutoff = now - timedelta(hours=24)

        devices = list(
            db.scalars(
                select(Tr069CpeDevice).where(Tr069CpeDevice.is_active.is_(True))
            ).all()
        )

        healthy = 0
        stale = 0
        stale_serials = []

        for device in devices:
            if device.last_inform_at and device.last_inform_at > stale_cutoff:
                healthy += 1
            else:
                stale += 1
                stale_serials.append(device.serial_number or str(device.id))

        if stale > 0:
            try:
                from app.services.events import emit_event
                from app.services.events.types import EventType

                emit_event(
                    db,
                    EventType.tr069_device_stale,
                    {
                        "stale_count": stale,
                        "stale_devices": stale_serials[:20],
                    },
                    actor="system",
                )
            except Exception as e:
                logger.warning("Failed to emit tr069_device_stale event: %s", e)

        logger.info(
            "TR-069 health check: %d total, %d healthy, %d stale",
            len(devices),
            healthy,
            stale,
        )
        return {
            "total_checked": len(devices),
            "healthy": healthy,
            "stale": stale,
            "errors": 0,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.tr069.cleanup_tr069_records")
def cleanup_tr069_records() -> dict[str, int]:
    """Clean up old TR-069 sessions and completed jobs.

    Deletes sessions older than 30 days and completed/failed jobs older
    than 90 days.

    Returns:
        Stats: {sessions_cleaned, jobs_cleaned}.
    """
    logger.info("Starting TR-069 record cleanup")
    db = SessionLocal()
    try:
        now = datetime.now(UTC)
        session_cutoff = now - timedelta(days=30)
        job_cutoff = now - timedelta(days=90)

        # Clean old sessions
        old_sessions = list(
            db.scalars(
                select(Tr069Session).where(Tr069Session.created_at < session_cutoff)
            ).all()
        )
        for session in old_sessions:
            db.delete(session)
        sessions_cleaned = len(old_sessions)

        # Clean old completed/failed/canceled jobs
        old_jobs = list(
            db.scalars(
                select(Tr069Job).where(
                    Tr069Job.created_at < job_cutoff,
                    Tr069Job.status.in_(
                        [
                            Tr069JobStatus.succeeded,
                            Tr069JobStatus.failed,
                            Tr069JobStatus.canceled,
                        ]
                    ),
                )
            ).all()
        )
        for job in old_jobs:
            db.delete(job)
        jobs_cleaned = len(old_jobs)

        if sessions_cleaned or jobs_cleaned:
            db.commit()

        logger.info(
            "TR-069 cleanup: %d sessions removed, %d jobs removed",
            sessions_cleaned,
            jobs_cleaned,
        )
        return {"sessions_cleaned": sessions_cleaned, "jobs_cleaned": jobs_cleaned}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _emit_job_event(db, emit_event, event_type, job: Tr069Job) -> None:
    """Emit a TR-069 job event (helper)."""
    try:
        emit_event(
            db,
            event_type,
            {
                "job_id": str(job.id),
                "device_id": str(job.device_id),
                "command": job.command,
                "status": job.status.value,
                "error": job.error,
            },
            actor="system",
        )
    except Exception as e:
        logger.warning("Failed to emit TR-069 job event: %s", e)


@celery_app.task(name="app.tasks.tr069.execute_bulk_action")
def execute_bulk_action(
    device_ids: list[str],
    action: str,
    params: dict | None = None,
) -> dict[str, int]:
    """Execute an action on multiple TR-069 devices.

    Args:
        device_ids: List of Tr069CpeDevice UUIDs.
        action: Action name (refresh, reboot, factory_reset, config_push, firmware).
        params: Additional parameters for the action.

    Returns:
        Statistics dict with processed/errors/skipped counts.
    """
    from app.services.common import coerce_uuid
    from app.services.tr069 import Jobs

    logger.info("Starting bulk TR-069 %s for %d device(s)", action, len(device_ids))
    db = SessionLocal()
    processed = 0
    errors = 0
    skipped = 0
    params = params or {}

    try:
        for device_id_str in device_ids:
            try:
                device_id = coerce_uuid(device_id_str)
                device = db.get(Tr069CpeDevice, device_id)
                if not device:
                    logger.warning("TR-069 device %s not found, skipping", device_id_str)
                    skipped += 1
                    continue

                # Build and execute job based on action type
                job = _create_bulk_job(db, device_id, action, params)
                if job is None:
                    skipped += 1
                    continue

                result = Jobs.execute(db, str(job.id))
                if result.status == Tr069JobStatus.succeeded:
                    processed += 1
                else:
                    logger.warning(
                        "Bulk %s failed for TR-069 device %s: %s",
                        action,
                        device_id_str,
                        result.error,
                    )
                    errors += 1
            except Exception as exc:
                logger.error(
                    "Bulk %s error for TR-069 device %s: %s", action, device_id_str, exc
                )
                errors += 1

        db.commit()
    except Exception as exc:
        logger.error("Bulk TR-069 action %s failed: %s", action, exc)
        db.rollback()
        raise
    finally:
        db.close()

    stats = {"processed": processed, "errors": errors, "skipped": skipped}
    logger.info("Bulk TR-069 %s complete: %s", action, stats)
    return stats


def _create_bulk_job(db, device_id, action: str, params: dict) -> Tr069Job | None:
    """Create a TR-069 job for a bulk action."""
    from uuid import UUID

    from app.schemas.tr069 import Tr069JobCreate
    from app.services.tr069 import Jobs

    # Map action to job definition
    job_definitions: dict[str, dict] = {
        "refresh": {
            "name": "Refresh Parameters",
            "command": "refreshObject",
            "payload": {"objectName": "Device."},
        },
        "reboot": {
            "name": "Reboot Device",
            "command": "reboot",
            "payload": None,
        },
        "factory_reset": {
            "name": "Factory Reset",
            "command": "factoryReset",
            "payload": None,
        },
    }

    if action in job_definitions:
        defn = job_definitions[action]
        payload = Tr069JobCreate(
            device_id=device_id if isinstance(device_id, UUID) else UUID(str(device_id)),
            name=defn["name"],
            command=defn["command"],
            payload=defn["payload"],
        )
        return Jobs.create(db, payload)

    if action == "config_push":
        # Config push requires parameter path and value in params
        parameter_path = params.get("parameter_path")
        parameter_value = params.get("parameter_value")
        if not parameter_path:
            logger.warning("config_push requires parameter_path")
            return None
        payload = Tr069JobCreate(
            device_id=device_id if isinstance(device_id, UUID) else UUID(str(device_id)),
            name="Config Push",
            command="setParameterValues",
            payload={
                "parameterValues": [[parameter_path, parameter_value, "xsd:string"]],
            },
        )
        return Jobs.create(db, payload)

    if action == "firmware":
        # Firmware update requires URL in params
        firmware_url = params.get("firmware_url")
        if not firmware_url:
            logger.warning("firmware requires firmware_url")
            return None
        task_payload: dict = {
            "fileType": "1 Firmware Upgrade Image",
            "url": firmware_url,
        }
        if params.get("filename"):
            task_payload["filename"] = params["filename"]
        payload = Tr069JobCreate(
            device_id=device_id if isinstance(device_id, UUID) else UUID(str(device_id)),
            name="Firmware Update",
            command="download",
            payload=task_payload,
        )
        return Jobs.create(db, payload)

    logger.warning("Unknown bulk action: %s", action)
    return None


@celery_app.task(name="app.tasks.tr069.refresh_ont_runtime_data")
def refresh_ont_runtime_data(batch_size: int = 50) -> dict[str, int]:
    """Periodically refresh TR-069 runtime data for ONTs.

    Fetches TR-069 parameters from GenieACS and persists observed runtime
    fields (WAN IP, PPPoE status, WiFi clients, etc.) to OntUnit records.

    Only processes ONTs that have TR-069/GenieACS configured and haven't
    been updated recently (>1 hour stale).

    Args:
        batch_size: Maximum ONTs to process per run.

    Returns:
        Stats: {processed, updated, errors, skipped}.
    """
    from app.models.network import OntUnit
    from app.services.network.ont_tr069 import ont_tr069

    logger.info("Starting TR-069 ONT runtime refresh (batch_size=%d)", batch_size)
    db = SessionLocal()
    try:
        now = datetime.now(UTC)
        stale_cutoff = now - timedelta(hours=1)

        # Find ONTs that have TR-069 links and need runtime refresh
        # Only process ONTs with genieacs_device_id (real TR-069 devices)
        stmt = (
            select(OntUnit)
            .join(Tr069CpeDevice, Tr069CpeDevice.ont_unit_id == OntUnit.id)
            .where(OntUnit.is_active.is_(True))
            .where(Tr069CpeDevice.is_active.is_(True))
            .where(Tr069CpeDevice.genieacs_device_id.isnot(None))
            .where(
                (OntUnit.observed_runtime_updated_at.is_(None))
                | (OntUnit.observed_runtime_updated_at < stale_cutoff)
            )
            .order_by(OntUnit.observed_runtime_updated_at.asc().nulls_first())
            .limit(batch_size)
        )
        onts = list(db.scalars(stmt).all())

        if not onts:
            logger.info("No ONTs need runtime refresh")
            return {"processed": 0, "updated": 0, "errors": 0, "skipped": 0}

        processed = 0
        updated = 0
        errors = 0
        skipped = 0

        for ont in onts:
            try:
                processed += 1
                summary = ont_tr069.get_device_summary(
                    db, str(ont.id), persist_observed_runtime=True
                )
                if summary.available:
                    updated += 1
                elif summary.error:
                    # Not an error if device simply isn't TR-069 managed
                    if "not managed" in (summary.error or "").lower():
                        skipped += 1
                    else:
                        logger.debug(
                            "TR-069 runtime fetch failed for ONT %s: %s",
                            ont.serial_number,
                            summary.error,
                        )
                        errors += 1
                else:
                    skipped += 1
            except Exception as exc:
                logger.warning(
                    "Error refreshing TR-069 runtime for ONT %s: %s",
                    ont.serial_number,
                    exc,
                )
                errors += 1

        logger.info(
            "TR-069 ONT runtime refresh: %d processed, %d updated, %d errors, %d skipped",
            processed,
            updated,
            errors,
            skipped,
        )
        return {
            "processed": processed,
            "updated": updated,
            "errors": errors,
            "skipped": skipped,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
