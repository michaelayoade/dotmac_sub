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
