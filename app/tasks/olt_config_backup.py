"""Celery task for periodic OLT running-config backup.

Connects to each active OLT via SNMP to retrieve the running
configuration and stores it as a timestamped text file.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.network import OltConfigBackup, OltConfigBackupType, OLTDevice
from app.services import backup_alerts
from app.services.credential_crypto import decrypt_credential

logger = logging.getLogger(__name__)

BACKUP_DIR = Path("/app/uploads/olt_config_backups")


def _resolve_snmp_community(db, olt: OLTDevice) -> str | None:
    """Resolve SNMP community string for an OLT.

    Checks the OLT's own snmp_ro_community first, then falls back to a
    linked NetworkDevice record.
    """
    from sqlalchemy import select

    from app.models.network_monitoring import NetworkDevice

    # 1. Prefer SNMP community stored directly on the OLT device
    raw_olt_community = getattr(olt, "snmp_ro_community", None)
    if raw_olt_community:
        raw_olt_community = raw_olt_community.strip()
    if raw_olt_community:
        return decrypt_credential(raw_olt_community)

    # 2. Fallback: linked NetworkDevice
    linked = None
    if olt.mgmt_ip:
        linked = db.scalars(
            select(NetworkDevice).where(NetworkDevice.mgmt_ip == olt.mgmt_ip).limit(1)
        ).first()
    if linked is None and olt.hostname:
        linked = db.scalars(
            select(NetworkDevice).where(NetworkDevice.hostname == olt.hostname).limit(1)
        ).first()
    if linked and linked.snmp_enabled:
        raw_community = (linked.snmp_community or "").strip() or None
        if raw_community:
            return decrypt_credential(raw_community)
    return None


def _fetch_running_config(olt: OLTDevice, community_str: str | None = None) -> str | None:
    """Fetch running config from an OLT via SNMP sysDescr + entPhysicalTable.

    Returns the config text or None if unreachable.
    """
    if not olt.mgmt_ip:
        return None

    try:
        from pysnmp.hlapi import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            getCmd,
        )

        # Gather basic info via SNMP
        engine = SnmpEngine()
        community = CommunityData(community_str or "public", mpModel=1)  # noqa: S508
        target = UdpTransportTarget((olt.mgmt_ip, 161), timeout=10, retries=1)

        oids = [
            "1.3.6.1.2.1.1.1.0",  # sysDescr
            "1.3.6.1.2.1.1.3.0",  # sysUpTime
            "1.3.6.1.2.1.1.5.0",  # sysName
            "1.3.6.1.2.1.1.6.0",  # sysLocation
        ]

        lines = [
            f"# OLT Config Snapshot: {olt.name}",
            f"# IP: {olt.mgmt_ip}",
            f"# Vendor: {olt.vendor or 'unknown'}",
            f"# Model: {olt.model or 'unknown'}",
            f"# Serial: {olt.serial_number or 'unknown'}",
            f"# Captured: {datetime.now(UTC).isoformat()}",
            "",
        ]

        for oid in oids:
            error_indication, error_status, _error_index, var_binds = next(
                getCmd(
                    engine,
                    community,
                    target,
                    ContextData(),
                    ObjectType(ObjectIdentity(oid)),
                )
            )
            if error_indication or error_status:
                continue
            for var_bind in var_binds:
                lines.append(f"{var_bind[0].prettyPrint()} = {var_bind[1].prettyPrint()}")

        return "\n".join(lines) + "\n"

    except ImportError:
        logger.warning("pysnmp not installed — skipping SNMP config fetch for %s", olt.name)
        return None
    except Exception as e:
        logger.error("Failed to fetch config from OLT %s (%s): %s", olt.name, olt.mgmt_ip, e)
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
    olt_ids = list(
        db.scalars(
            select(OltConfigBackup.olt_device_id).distinct()
        ).all()
    )
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
                logger.warning("Failed to delete backup file %s: %s", backup.file_path, e)
            db.delete(backup)
            cleaned += 1

    if cleaned:
        db.commit()
        logger.info("Cleaned up %d old OLT config backups", cleaned)

    return cleaned


@celery_app.task(name="app.tasks.olt_config_backup.backup_all_olts")
def backup_all_olts() -> dict[str, int]:
    """Backup running config for all active OLTs."""
    logger.info("Starting OLT config backup run")
    db = SessionLocal()
    backed_up = 0
    errors = 0
    skipped = 0

    try:
        from sqlalchemy import select

        olts = list(db.scalars(select(OLTDevice).where(OLTDevice.is_active.is_(True))).all())

        for olt in olts:
            community_str = _resolve_snmp_community(db, olt)
            config_text = _fetch_running_config(olt, community_str=community_str)
            if config_text is None:
                skipped += 1
                backup_alerts.queue_backup_failure_notification(
                    db,
                    device_kind="olt",
                    device_name=olt.name,
                    device_ip=olt.mgmt_ip,
                    error_message="Could not fetch running configuration",
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
    return {"backed_up": backed_up, "errors": errors, "skipped": skipped, "cleaned": cleaned}
