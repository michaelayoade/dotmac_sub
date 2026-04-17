"""Celery tasks for TR-069 background operations.

Handles periodic device sync from GenieACS, queued job execution with retry,
device health monitoring, and session/job retention cleanup.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError

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


def _is_psycopg_autocommit_state_error(exc: ProgrammingError) -> bool:
    """Return true for stale pooled psycopg connections stuck in a transaction."""
    return "can't change 'autocommit' now" in str(exc).lower()


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
        total_local_created = 0
        total_local_reactivated = 0
        errors = 0

        for server in servers:
            try:
                result = CpeDevices.sync_from_genieacs(db, str(server.id))
                total_created += result.get("created", 0)
                total_updated += result.get("updated", 0)
                total_local_created += result.get("local_created", 0)
                total_local_reactivated += result.get("local_reactivated", 0)
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
            "TR-069 sync complete: %d servers, %d created, %d updated, %d local created, %d local reactivated, %d errors",
            synced,
            total_created,
            total_updated,
            total_local_created,
            total_local_reactivated,
            errors,
        )
        return {
            "servers_synced": synced,
            "total_created": total_created,
            "total_updated": total_updated,
            "total_local_created": total_local_created,
            "total_local_reactivated": total_local_reactivated,
            "errors": errors,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.tr069.wait_for_ont_bootstrap")
def wait_for_ont_bootstrap(
    ont_id: str,
    operation_id: str | None = None,
    service_retry_count: int = 0,
) -> dict[str, object]:
    """Wait for an ONT to become resolvable in GenieACS after TR-069 binding."""
    from app.services.network.ont_provision_steps import (
        apply_saved_service_config,
        wait_tr069_bootstrap,
    )
    from app.services.network_operations import network_operations

    logger.info("Starting TR-069 bootstrap wait for ONT %s", ont_id)
    db = SessionLocal()
    try:
        if operation_id:
            network_operations.mark_running(db, operation_id)
            db.commit()

        result = wait_tr069_bootstrap(db, ont_id)
        apply_result = None
        if result.success:
            apply_result = apply_saved_service_config(db, ont_id)
        service_waiting = bool(apply_result.waiting) if apply_result else False
        payload = {
            "step_name": result.step_name,
            "success": result.success
            and (apply_result.success if apply_result else True)
            and not service_waiting,
            "message": result.message,
            "duration_ms": result.duration_ms,
            "waiting": result.waiting or service_waiting,
            "data": result.data or {},
        }
        if apply_result is not None:
            payload["service_config"] = {
                "step_name": apply_result.step_name,
                "success": apply_result.success,
                "message": apply_result.message,
                "duration_ms": apply_result.duration_ms,
                "waiting": apply_result.waiting,
                "skipped": apply_result.skipped,
                "data": apply_result.data or {},
            }
            if apply_result.message:
                payload["message"] = f"{result.message} {apply_result.message}"

        if operation_id:
            if payload["success"]:
                network_operations.mark_succeeded(
                    db,
                    operation_id,
                    output_payload=payload,
                )
            elif payload["waiting"] and service_retry_count < 3:
                network_operations.mark_waiting(
                    db,
                    operation_id,
                    str(payload["message"]),
                )
                from app.celery_app import enqueue_celery_task

                enqueue_celery_task(
                    "app.tasks.tr069.wait_for_ont_bootstrap",
                    args=[ont_id, operation_id, service_retry_count + 1],
                    correlation_id=f"tr069_bootstrap:{ont_id}",
                    source="ont_provision_step_retry",
                    countdown=60,
                )
            else:
                network_operations.mark_failed(
                    db,
                    operation_id,
                    str(payload["message"]),
                    output_payload=payload,
                )
            db.commit()
        else:
            db.rollback()

        return payload
    except Exception as exc:
        db.rollback()
        if operation_id:
            try:
                network_operations.mark_failed(db, operation_id, str(exc))
                db.commit()
            except Exception:
                db.rollback()
                logger.warning(
                    "Failed to mark TR-069 bootstrap operation %s failed",
                    operation_id,
                    exc_info=True,
                )
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
            "payload": {"objectName": "InternetGatewayDevice."},
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
        try:
            onts = list(db.scalars(stmt).all())
        except ProgrammingError as exc:
            if not _is_psycopg_autocommit_state_error(exc):
                raise

            logger.warning(
                "TR-069 runtime refresh hit a stale DB connection; invalidating and retrying once"
            )
            db.invalidate()
            db.close()
            db = SessionLocal()
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


@celery_app.task(name="app.tasks.tr069.cleanup_stale_genieacs_tasks")
def cleanup_stale_genieacs_tasks(
    max_age_hours: int = 6,
    dry_run: bool = False,
) -> dict[str, int]:
    """Clean up stale pending tasks and faults from GenieACS.

    Tasks stuck in pending state for longer than max_age_hours are deleted,
    along with their associated faults. This prevents blocking inform loops
    caused by permanently failing tasks (e.g., invalid parameter values).

    Args:
        max_age_hours: Delete tasks older than this (default 6 hours).
        dry_run: If True, report what would be deleted without deleting.

    Returns:
        Stats: {tasks_deleted, faults_deleted, servers_processed, errors}.
    """
    from app.services.genieacs import GenieACSClient, GenieACSError

    logger.info(
        "Starting GenieACS stale task cleanup (max_age=%dh, dry_run=%s)",
        max_age_hours,
        dry_run,
    )
    db = SessionLocal()
    try:
        servers = list(
            db.scalars(
                select(Tr069AcsServer).where(Tr069AcsServer.is_active.is_(True))
            ).all()
        )
        if not servers:
            return {
                "tasks_deleted": 0,
                "faults_deleted": 0,
                "servers_processed": 0,
                "errors": 0,
            }

        total_tasks = 0
        total_faults = 0
        errors = 0

        for server in servers:
            if not server.base_url:
                continue
            try:
                client = GenieACSClient(server.base_url)

                # Delete stale tasks
                result = client.delete_stale_tasks(
                    older_than=timedelta(hours=max_age_hours),
                    dry_run=dry_run,
                )
                total_tasks += result.get("deleted", 0) if not dry_run else result.get("matched", 0)

                # Clear associated faults for stale tasks
                if not dry_run:
                    faults = client.list_faults()
                    now = datetime.now(UTC)
                    cutoff = now - timedelta(hours=max_age_hours)
                    for fault in faults:
                        ts_str = fault.get("timestamp", "")
                        if not ts_str:
                            continue
                        try:
                            fault_time = datetime.fromisoformat(
                                ts_str.replace("Z", "+00:00")
                            )
                            if fault_time < cutoff:
                                fault_id = fault.get("_id", "")
                                if fault_id:
                                    client.delete_fault(fault_id)
                                    total_faults += 1
                        except (ValueError, GenieACSError) as exc:
                            logger.debug("Fault cleanup error: %s", exc)

            except GenieACSError as exc:
                logger.warning(
                    "GenieACS cleanup failed for server %s: %s", server.name, exc
                )
                errors += 1

        logger.info(
            "GenieACS cleanup complete: %d tasks, %d faults deleted from %d servers (%d errors)",
            total_tasks,
            total_faults,
            len(servers),
            errors,
        )
        return {
            "tasks_deleted": total_tasks,
            "faults_deleted": total_faults,
            "servers_processed": len(servers),
            "errors": errors,
        }
    finally:
        db.close()


@celery_app.task(name="app.tasks.tr069.scrape_genieacs_metrics")
def scrape_genieacs_metrics() -> dict[str, int]:
    """Scrape GenieACS NBI and emit fleet metrics to VictoriaMetrics.

    Metrics:
      tr069_pending_tasks{age_bucket}           — queued tasks by age
      tr069_faults{code}                        — current faults grouped by fault code
      tr069_cpe_inform_age{bucket}              — CPEs bucketed by time since last Inform
      tr069_online_silent_total                 — ONTs OLT-online but ACS-silent >15min
    """
    import os

    import httpx

    from app.models.network import OntUnit, OnuOnlineStatus
    from app.services.monitoring_metrics import push_metrics_to_victoriametrics

    nbi_url = os.getenv("GENIEACS_NBI_URL", "http://genieacs:7557")
    now = datetime.now(UTC)

    lines: list[str] = []
    stats = {"pending": 0, "faults": 0, "online_silent": 0, "cpes": 0}

    try:
        with httpx.Client(base_url=nbi_url, timeout=15) as client:
            tasks = client.get("/tasks/?query=%7B%7D").json()
            faults = client.get("/faults/?query=%7B%7D").json()
            devices = client.get(
                "/devices/?projection=_id,_lastInform"
            ).json()
    except Exception as exc:
        logger.warning("GenieACS scrape failed: %s", exc)
        return {"error": str(exc)}

    # pending tasks by age bucket
    task_buckets = {"le_15m": 0, "le_1h": 0, "le_6h": 0, "gt_6h": 0}
    for t in tasks:
        ts_str = t.get("timestamp", "").replace("Z", "+00:00")
        try:
            age = (now - datetime.fromisoformat(ts_str)).total_seconds()
        except Exception:
            continue
        if age <= 900:
            task_buckets["le_15m"] += 1
        elif age <= 3600:
            task_buckets["le_1h"] += 1
        elif age <= 21600:
            task_buckets["le_6h"] += 1
        else:
            task_buckets["gt_6h"] += 1
    for bucket, count in task_buckets.items():
        lines.append(f"tr069_pending_tasks{{age_bucket=\"{bucket}\"}} {count}")
    stats["pending"] = sum(task_buckets.values())

    # faults by code
    fault_codes: dict[str, int] = {}
    for f in faults:
        code = str(f.get("code", "unknown"))
        fault_codes[code] = fault_codes.get(code, 0) + 1
    for code, count in fault_codes.items():
        safe_code = code.replace("\"", "").replace("\\", "")
        lines.append(f"tr069_faults{{code=\"{safe_code}\"}} {count}")
    stats["faults"] = sum(fault_codes.values())

    # CPE inform age buckets
    inform_buckets = {"fresh_1h": 0, "stale_6h": 0, "very_stale_24h": 0, "offline_gt_24h": 0, "never": 0}
    silent_ids: set[str] = set()
    for d in devices:
        li = d.get("_lastInform")
        if not li:
            inform_buckets["never"] += 1
            silent_ids.add(d["_id"])
            continue
        try:
            age = (now - datetime.fromisoformat(li.replace("Z", "+00:00"))).total_seconds()
        except Exception:
            continue
        if age <= 3600:
            inform_buckets["fresh_1h"] += 1
        elif age <= 21600:
            inform_buckets["stale_6h"] += 1
        elif age <= 86400:
            inform_buckets["very_stale_24h"] += 1
        else:
            inform_buckets["offline_gt_24h"] += 1
        if age > 900:
            silent_ids.add(d["_id"])
    for bucket, count in inform_buckets.items():
        lines.append(f"tr069_cpe_inform_age{{bucket=\"{bucket}\"}} {count}")
    stats["cpes"] = len(devices)

    # online-silent: ONT online per OLT but ACS silent >15min
    db = SessionLocal()
    try:
        online_serials = {
            s for s, in db.execute(
                select(OntUnit.serial_number).where(
                    OntUnit.is_active.is_(True),
                    OntUnit.online_status == OnuOnlineStatus.online,
                    OntUnit.serial_number.is_not(None),
                )
            ).all()
        }
    finally:
        db.close()

    silent_serials = {sid.split("-")[-1] for sid in silent_ids}
    online_silent = len(online_serials & silent_serials)
    lines.append(f"tr069_online_silent_total {online_silent}")
    stats["online_silent"] = online_silent

    push_metrics_to_victoriametrics(lines)

    logger.info(
        "GenieACS metrics scrape: pending=%d faults=%d cpes=%d online_silent=%d",
        stats["pending"], stats["faults"], stats["cpes"], stats["online_silent"],
    )
    return stats
