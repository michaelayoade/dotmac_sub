"""Celery task for periodic OLT running-config backup.

Connects to each active OLT over SSH to retrieve the full running
configuration and stores it as a timestamped text file.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.models.network import OltConfigBackup, OltConfigBackupType, OLTDevice
from app.services import backup_alerts
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

BACKUP_DIR = Path("/app/uploads/olt_config_backups")


def _fetch_running_config_via_ssh(olt: OLTDevice) -> str | None:
    """Fetch full running configuration from an OLT via SSH.

    Uses `display current-configuration` which returns the complete config
    (Tconts, GEM ports, service-ports, VLANs, interfaces, etc.).

    Returns the config text or None if SSH is unavailable.
    """
    try:
        from app.services.network.olt_protocol_adapters import get_protocol_adapter

        result = get_protocol_adapter(olt).fetch_running_config()
        raw_config_text = result.data.get("config_text") if result.success else ""
        config_text = raw_config_text if isinstance(raw_config_text, str) else ""
        if result.success and config_text:
            # Add metadata header
            header = (
                f"# OLT Full Running Config: {olt.name}\n"
                f"# IP: {olt.mgmt_ip}\n"
                f"# Vendor: {olt.vendor or 'unknown'}\n"
                f"# Model: {olt.model or 'unknown'}\n"
                f"# Serial: {olt.serial_number or 'unknown'}\n"
                f"# Method: SSH (display current-configuration)\n"
                f"# Captured: {datetime.now(UTC).isoformat()}\n"
                f"#\n"
            )
            return header + config_text + "\n"
        logger.warning("SSH config fetch for OLT %s: %s", olt.name, result.message)
        return None
    except Exception as e:
        logger.warning("SSH config backup failed for OLT %s: %s", olt.name, e)
        return None


def _cleanup_old_backups(db, max_age_days: int = 90, max_per_olt: int = 50) -> int:
    """Remove old backups beyond retention limits.

    Deletes backups older than max_age_days AND keeps at most max_per_olt
    backups per OLT (newest retained).
    """
    from sqlalchemy import select

    cleaned = 0
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)

    # 1. Delete by age
    old_backups = list(
        db.scalars(
            select(OltConfigBackup).where(OltConfigBackup.created_at < cutoff)
        ).all()
    )
    for backup in old_backups:
        try:
            filepath = BACKUP_DIR / backup.file_path
            if filepath.exists():
                filepath.unlink()
        except OSError as e:
            logger.warning("Failed to delete backup file %s: %s", backup.file_path, e)
        db.delete(backup)
        cleaned += 1

    # 2. Per-OLT cap: keep only the newest max_per_olt backups
    olt_ids = list(db.scalars(select(OltConfigBackup.olt_device_id).distinct()).all())
    for olt_id in olt_ids:
        backups = list(
            db.scalars(
                select(OltConfigBackup)
                .where(OltConfigBackup.olt_device_id == olt_id)
                .order_by(OltConfigBackup.created_at.desc())
                .offset(max_per_olt)
            ).all()
        )
        for backup in backups:
            try:
                filepath = BACKUP_DIR / backup.file_path
                if filepath.exists():
                    filepath.unlink()
            except OSError as e:
                logger.warning(
                    "Failed to delete backup file %s: %s", backup.file_path, e
                )
            db.delete(backup)
            cleaned += 1

    if cleaned:
        db.commit()
        logger.info("Cleaned up %d old OLT config backups", cleaned)

    return cleaned


@celery_app.task(
    name="app.tasks.olt_config_backup.backup_all_olts",
    # Serial SSH across the whole fleet can exceed the global 840/900s default
    # and be hard-killed mid-run. Give it generous-but-bounded limits and commit
    # whatever finished when the soft limit hits (handled below).
    soft_time_limit=3000,
    time_limit=3300,
)
def backup_all_olts() -> dict[str, int]:
    """Backup running config for all active OLTs."""
    logger.info("Starting OLT config backup run")
    db = db_session_adapter.create_session()
    backed_up = 0
    errors = 0
    skipped = 0
    cleaned = 0
    error_details: list[dict[str, str | None]] = []

    try:
        from sqlalchemy import select

        olts = list(
            db.scalars(select(OLTDevice).where(OLTDevice.is_active.is_(True))).all()
        )

        for olt in olts:
            try:
                config_text = _fetch_running_config_via_ssh(olt)
                if config_text is None:
                    skipped += 1
                    error_details.append(
                        {
                            "olt": olt.name,
                            "mgmt_ip": olt.mgmt_ip,
                            "error": "Could not fetch running configuration",
                        }
                    )
                    backup_alerts.queue_backup_failure_notification(
                        db,
                        device_kind="olt",
                        device_name=olt.name,
                        device_ip=olt.mgmt_ip,
                        error_message="Could not fetch running configuration",
                        run_type="scheduled",
                    )
                    continue
            except Exception as e:
                logger.error("Failed to fetch backup for OLT %s: %s", olt.name, e)
                errors += 1
                error_details.append(
                    {"olt": olt.name, "mgmt_ip": olt.mgmt_ip, "error": str(e)}
                )
                backup_alerts.queue_backup_failure_notification(
                    db,
                    device_kind="olt",
                    device_name=olt.name,
                    device_ip=olt.mgmt_ip,
                    error_message=str(e),
                    run_type="scheduled",
                )
                continue

            try:
                # Write to file
                timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
                safe_name = olt.name.replace(" ", "_").replace("/", "_")[:60]
                filename = f"{safe_name}_{timestamp}.txt"
                olt_dir = BACKUP_DIR / str(olt.id)
                olt_dir.mkdir(parents=True, exist_ok=True)
                filepath = olt_dir / filename
                filepath.write_text(config_text)

                # Record in DB with integrity hash
                config_bytes = config_text.encode()
                file_hash = hashlib.sha256(config_bytes).hexdigest()
                backup = OltConfigBackup(
                    id=uuid.uuid4(),
                    olt_device_id=olt.id,
                    backup_type=OltConfigBackupType.auto,
                    file_path=str(filepath.relative_to(BACKUP_DIR)),
                    file_size_bytes=len(config_bytes),
                    file_hash=file_hash,
                )
                db.add(backup)
                backed_up += 1

            except Exception as e:
                logger.error("Failed to save backup for OLT %s: %s", olt.name, e)
                errors += 1
                backup_alerts.queue_backup_failure_notification(
                    db,
                    device_kind="olt",
                    device_name=olt.name,
                    device_ip=olt.mgmt_ip,
                    error_message=str(e),
                    run_type="scheduled",
                )

        db.commit()

        # Retention cleanup: remove backups older than configured age
        cleaned = _cleanup_old_backups(db, max_age_days=90, max_per_olt=50)

    except SoftTimeLimitExceeded:
        # Out of time — keep the backups that finished rather than losing the
        # whole run. Retention cleanup is skipped until the next run.
        logger.warning(
            "olt_config_backup soft time limit hit; committing %d backups", backed_up
        )
        try:
            db.commit()
        except Exception:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    logger.info(
        "OLT config backup complete: backed_up=%d, errors=%d, skipped=%d, cleaned=%d",
        backed_up,
        errors,
        skipped,
        cleaned,
    )
    return {
        "backed_up": backed_up,
        "errors": errors,
        "skipped": skipped,
        "cleaned": cleaned,
        "error_details": error_details,
    }
