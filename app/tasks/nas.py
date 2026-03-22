"""Celery tasks for NAS device management.

Handles backup retention cleanup, scheduled config backups,
capacity tracking, and device health checks.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.catalog import (
    NasDevice,
    NasDeviceStatus,
    Subscription,
    SubscriptionStatus,
)

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.nas.cleanup_nas_backups")
def cleanup_nas_backups() -> dict[str, int]:
    """Remove old NAS config backups beyond retention limits."""
    logger.info("Starting cleanup_nas_backups")
    db = SessionLocal()
    try:
        from app.services.nas.backups import NasConfigBackups

        result = NasConfigBackups.cleanup_retention(db)
        logger.info("Completed cleanup_nas_backups: %s", result)
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.nas.run_scheduled_backups")
def run_scheduled_backups() -> dict[str, int]:
    """Trigger config backups for NAS devices with backup_enabled=True.

    Reads each device's backup_schedule (cron expression stored as text)
    and last_backup_at timestamp. Devices that are due for backup are
    processed via DeviceProvisioner.backup_config().

    Returns:
        Stats: {attempted, succeeded, failed, skipped}.
    """
    logger.info("Starting scheduled NAS config backups")
    db = SessionLocal()
    try:
        devices = list(
            db.scalars(
                select(NasDevice).where(
                    NasDevice.is_active.is_(True),
                    NasDevice.backup_enabled.is_(True),
                    NasDevice.status == NasDeviceStatus.active,
                )
            ).all()
        )

        if not devices:
            logger.info("No NAS devices with backup_enabled")
            return {"attempted": 0, "succeeded": 0, "failed": 0, "skipped": 0}

        from app.services.nas.provisioner import DeviceProvisioner

        now = datetime.now(UTC)
        attempted = 0
        succeeded = 0
        failed = 0
        skipped = 0

        for device in devices:
            # Check if backup is due based on schedule interval
            if device.last_backup_at:
                interval = _parse_backup_interval(device.backup_schedule)
                if (now - device.last_backup_at) < interval:
                    skipped += 1
                    continue

            attempted += 1
            try:
                DeviceProvisioner.backup_config(db, device.id, triggered_by="scheduled")
                device.last_backup_at = now
                db.commit()
                succeeded += 1
            except Exception as e:
                logger.error(
                    "Scheduled backup failed for NAS %s (%s): %s",
                    device.name, device.id, e,
                )
                db.rollback()
                failed += 1

                # Notify on failure
                try:
                    from app.services import backup_alerts

                    backup_alerts.queue_backup_failure_notification(
                        db,
                        device_kind="nas",
                        device_name=device.name,
                        device_ip=device.management_ip or device.ip_address,
                        error_message=str(e),
                        run_type="scheduled",
                    )
                except Exception:
                    logger.warning(
                        "Failed to queue NAS backup failure notification for %s",
                        device.id,
                        exc_info=True,
                    )

        logger.info(
            "Scheduled NAS backups: attempted=%d succeeded=%d failed=%d skipped=%d",
            attempted, succeeded, failed, skipped,
        )
        return {
            "attempted": attempted,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.nas.update_subscriber_counts")
def update_subscriber_counts() -> dict[str, int]:
    """Update current_subscriber_count on all active NAS devices.

    Counts active subscriptions per NAS device and updates the
    current_subscriber_count field. Logs a warning if any device
    exceeds its max_concurrent_subscribers threshold.

    Returns:
        Stats: {devices_updated, over_capacity}.
    """
    logger.info("Starting NAS subscriber count update")
    db = SessionLocal()
    try:
        devices = list(
            db.scalars(
                select(NasDevice).where(NasDevice.is_active.is_(True))
            ).all()
        )

        updated = 0
        over_capacity = 0

        for device in devices:
            count = db.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.provisioning_nas_device_id == device.id,
                    Subscription.status == SubscriptionStatus.active,
                )
            ) or 0

            if device.current_subscriber_count != count:
                device.current_subscriber_count = count
                updated += 1

            if (
                device.max_concurrent_subscribers
                and count > device.max_concurrent_subscribers
            ):
                over_capacity += 1
                logger.warning(
                    "NAS %s (%s) over capacity: %d/%d subscribers",
                    device.name, device.id, count, device.max_concurrent_subscribers,
                )

        if updated:
            db.commit()

        logger.info(
            "NAS subscriber counts: %d devices updated, %d over capacity",
            updated, over_capacity,
        )
        return {"devices_updated": updated, "over_capacity": over_capacity}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.nas.check_nas_health")
def check_nas_health() -> dict[str, int]:
    """Ping all active NAS devices and update health status.

    For each active NAS, pings the management IP. Updates last_seen_at
    on success. Marks devices as offline if unreachable for >1 hour.

    Returns:
        Stats: {total, reachable, unreachable, marked_offline}.
    """
    logger.info("Starting NAS health check")
    db = SessionLocal()
    try:
        from app.services import ping as ping_service

        devices = list(
            db.scalars(
                select(NasDevice).where(
                    NasDevice.is_active.is_(True),
                    NasDevice.status.in_([
                        NasDeviceStatus.active,
                        NasDeviceStatus.maintenance,
                    ]),
                )
            ).all()
        )

        now = datetime.now(UTC)
        offline_cutoff = now - timedelta(hours=1)
        reachable = 0
        unreachable = 0
        marked_offline = 0

        for device in devices:
            host = device.management_ip or device.ip_address
            if not host:
                unreachable += 1
                continue

            try:
                is_up, _latency = ping_service.run_ping(host, timeout_seconds=4)
            except Exception:
                is_up = False

            if is_up:
                device.last_seen_at = now
                reachable += 1
            else:
                unreachable += 1
                # Mark offline if not seen for over 1 hour
                if (
                    device.status == NasDeviceStatus.active
                    and (not device.last_seen_at or device.last_seen_at < offline_cutoff)
                ):
                    device.status = NasDeviceStatus.offline
                    marked_offline += 1
                    logger.warning(
                        "NAS %s (%s) marked offline — unreachable for >1 hour",
                        device.name, host,
                    )

        db.commit()

        logger.info(
            "NAS health check: %d total, %d reachable, %d unreachable, %d marked offline",
            len(devices), reachable, unreachable, marked_offline,
        )
        return {
            "total": len(devices),
            "reachable": reachable,
            "unreachable": unreachable,
            "marked_offline": marked_offline,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _parse_backup_interval(schedule: str | None) -> timedelta:
    """Parse a backup schedule string into a timedelta.

    Supports formats:
    - "daily" / "24h" → 24 hours
    - "weekly" / "7d" → 7 days
    - "12h" → 12 hours
    - "Nh" → N hours
    - "Nd" → N days
    - Fallback: 24 hours
    """
    if not schedule:
        return timedelta(hours=24)

    schedule = schedule.strip().lower()

    if schedule == "daily":
        return timedelta(hours=24)
    if schedule == "weekly":
        return timedelta(days=7)

    import re

    match = re.match(r"^(\d+)\s*(h|d)$", schedule)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit == "h":
            return timedelta(hours=value)
        if unit == "d":
            return timedelta(days=value)

    return timedelta(hours=24)
