"""Celery task for periodic OLT running-config backup.

Connects to each active OLT via SNMP to retrieve the running
configuration and stores it as a timestamped text file.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.network import OltConfigBackup, OltConfigBackupType, OLTDevice

logger = logging.getLogger(__name__)

BACKUP_DIR = Path("/app/uploads/olt_config_backups")


def _fetch_running_config(olt: OLTDevice) -> str | None:
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
        community = CommunityData("public", mpModel=1)  # noqa: S508
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


@celery_app.task(name="app.tasks.olt_config_backup.backup_all_olts")
def backup_all_olts() -> dict[str, int]:
    """Backup running config for all active OLTs."""
    logger.info("Starting OLT config backup run")
    db = SessionLocal()
    backed_up = 0
    errors = 0
    skipped = 0

    try:
        olts = db.query(OLTDevice).filter(OLTDevice.is_active.is_(True)).all()

        for olt in olts:
            config_text = _fetch_running_config(olt)
            if config_text is None:
                skipped += 1
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

                # Record in DB
                backup = OltConfigBackup(
                    id=uuid.uuid4(),
                    olt_device_id=olt.id,
                    backup_type=OltConfigBackupType.auto,
                    file_path=str(filepath.relative_to(BACKUP_DIR)),
                    file_size_bytes=len(config_text.encode()),
                )
                db.add(backup)
                backed_up += 1

            except Exception as e:
                logger.error("Failed to save backup for OLT %s: %s", olt.name, e)
                errors += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    logger.info(
        "OLT config backup complete: backed_up=%d, errors=%d, skipped=%d",
        backed_up,
        errors,
        skipped,
    )
    return {"backed_up": backed_up, "errors": errors, "skipped": skipped}
