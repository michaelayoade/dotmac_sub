"""Service helpers for admin OLT web routes."""

from __future__ import annotations

import logging
import os
import uuid
from difflib import unified_diff
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OltConfigBackup, OltConfigBackupType
from app.schemas.network import OLTDeviceCreate, OLTDeviceUpdate
from app.services import network as network_service
from app.services.audit_helpers import (
    diff_dicts,
    log_audit_event,
    model_to_dict,
)

logger = logging.getLogger(__name__)
_FALLBACK_OLT_BACKUP_DIR = Path("uploads/olt_config_backups")


def integrity_error_message(exc: Exception) -> str:
    """Map OLT integrity errors to user-facing strings."""
    message = str(exc)
    if "uq_olt_devices_hostname" in message:
        return "Hostname already exists"
    if "uq_olt_devices_mgmt_ip" in message:
        return "Management IP already exists"
    return "OLT device could not be saved due to a data conflict"


def parse_form_values(form: Mapping[str, Any]) -> dict[str, object]:
    """Parse OLT form values."""
    return {
        "name": form.get("name", "").strip(),
        "hostname": form.get("hostname", "").strip() or None,
        "mgmt_ip": form.get("mgmt_ip", "").strip() or None,
        "vendor": form.get("vendor", "").strip() or None,
        "model": form.get("model", "").strip() or None,
        "serial_number": form.get("serial_number", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
        "is_active": form.get("is_active") == "true",
    }


def validate_values(db: Session, values: dict[str, object], *, current_olt: OLTDevice | None = None) -> str | None:
    """Validate required fields and uniqueness."""
    if not values.get("name"):
        return "Name is required"
    hostname = values.get("hostname")
    mgmt_ip = values.get("mgmt_ip")
    if hostname:
        stmt = select(OLTDevice).where(OLTDevice.hostname == hostname)
        if current_olt:
            stmt = stmt.where(OLTDevice.id != current_olt.id)
        if db.scalars(stmt).first():
            return "Hostname already exists"
    if mgmt_ip:
        stmt = select(OLTDevice).where(OLTDevice.mgmt_ip == mgmt_ip)
        if current_olt:
            stmt = stmt.where(OLTDevice.id != current_olt.id)
        if db.scalars(stmt).first():
            return "Management IP already exists"
    return None


def create_payload(values: dict[str, object]) -> OLTDeviceCreate:
    """Build create payload from parsed values."""
    return OLTDeviceCreate.model_validate(values)


def update_payload(values: dict[str, object]) -> OLTDeviceUpdate:
    """Build update payload from parsed values."""
    return OLTDeviceUpdate.model_validate(values)


def create_olt(db: Session, values: dict[str, object]) -> tuple[OLTDevice | None, str | None]:
    """Create OLT and normalize integrity errors."""
    try:
        olt = network_service.olt_devices.create(db=db, payload=create_payload(values))
        return olt, None
    except IntegrityError as exc:
        logger.warning("OLT create integrity error: %s", exc)
        db.rollback()
        return None, integrity_error_message(exc)


def update_olt(db: Session, olt_id: str, values: dict[str, object]) -> tuple[OLTDevice | None, str | None]:
    """Update OLT and normalize integrity errors."""
    try:
        olt = network_service.olt_devices.update(
            db=db,
            device_id=olt_id,
            payload=update_payload(values),
        )
        return olt, None
    except IntegrityError as exc:
        logger.warning("OLT update integrity error for %s: %s", olt_id, exc)
        db.rollback()
        return None, integrity_error_message(exc)


def create_olt_with_audit(
    db: Session,
    request: Request,
    values: dict[str, object],
    actor_id: str | None,
) -> tuple[OLTDevice | None, str | None]:
    """Create OLT, log audit event, and return result."""
    olt, error = create_olt(db, values)
    if error or olt is None:
        return olt, error
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="olt",
        entity_id=str(olt.id),
        actor_id=actor_id,
        metadata={"name": olt.name, "mgmt_ip": olt.mgmt_ip or None},
    )
    return olt, None


def update_olt_with_audit(
    db: Session,
    request: Request,
    olt_id: str,
    before_obj: OLTDevice,
    values: dict[str, object],
    actor_id: str | None,
) -> tuple[OLTDevice | None, str | None]:
    """Update OLT, compute diff, log audit event, and return result."""
    before_snapshot = model_to_dict(before_obj)
    olt, error = update_olt(db, olt_id, values)
    if error or olt is None:
        return olt, error
    after_obj = network_service.olt_devices.get(db=db, device_id=olt_id)
    after_snapshot = model_to_dict(after_obj)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata_payload = {"changes": changes} if changes else None
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=actor_id,
        metadata=metadata_payload,
    )
    return olt, None


def get_olt_or_none(db: Session, olt_id: str) -> OLTDevice | None:
    """Get an OLT device, returning None instead of raising on 404."""
    try:
        return network_service.olt_devices.get(db=db, device_id=olt_id)
    except HTTPException:
        return None


def snapshot(values: dict[str, object]) -> SimpleNamespace:
    """Build simple object for form re-render on errors."""
    return SimpleNamespace(**values)


def _olt_backup_base_dir() -> Path:
    configured = os.getenv("OLT_BACKUP_DIR", "/app/uploads/olt_config_backups")
    candidate = Path(configured)
    if candidate.exists():
        return candidate
    return _FALLBACK_OLT_BACKUP_DIR


def _resolve_backup_file(file_path: str) -> Path:
    base = _olt_backup_base_dir().resolve()
    candidate = (base / file_path).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid backup path")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Backup file not found")
    return candidate


def list_olt_backups(
    db: Session,
    *,
    olt_id: str,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> list[OltConfigBackup]:
    try:
        olt_uuid = UUID(str(olt_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid OLT ID") from exc
    query = select(OltConfigBackup).where(OltConfigBackup.olt_device_id == olt_uuid)
    if start_at is not None:
        query = query.where(OltConfigBackup.created_at >= start_at)
    if end_at is not None:
        query = query.where(OltConfigBackup.created_at <= end_at)
    query = query.order_by(OltConfigBackup.created_at.desc())
    return list(db.scalars(query).all())


def get_olt_backup_or_none(db: Session, backup_id: str) -> OltConfigBackup | None:
    try:
        return db.get(OltConfigBackup, backup_id)
    except Exception:
        return None


def backup_file_path(backup: OltConfigBackup) -> Path:
    return _resolve_backup_file(backup.file_path)


def read_backup_preview(backup: OltConfigBackup, limit_chars: int = 120_000) -> str:
    path = backup_file_path(backup)
    return path.read_text(errors="replace")[:limit_chars]


def read_backup_content(backup: OltConfigBackup) -> str:
    path = backup_file_path(backup)
    return path.read_text(errors="replace")


def compare_olt_backups(
    db: Session,
    backup_id_1: str,
    backup_id_2: str,
) -> tuple[OltConfigBackup, OltConfigBackup, dict[str, object]]:
    backup1 = get_olt_backup_or_none(db, backup_id_1)
    backup2 = get_olt_backup_or_none(db, backup_id_2)
    if not backup1 or not backup2:
        raise HTTPException(status_code=404, detail="One or both backups not found")
    if backup1.olt_device_id != backup2.olt_device_id:
        raise HTTPException(status_code=400, detail="Backups must belong to the same OLT")

    text1 = read_backup_content(backup1)
    text2 = read_backup_content(backup2)
    lines1 = text1.splitlines()
    lines2 = text2.splitlines()
    diff_lines = list(
        unified_diff(
            lines1,
            lines2,
            fromfile=str(backup1.file_path),
            tofile=str(backup2.file_path),
            lineterm="",
        )
    )
    added_lines = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed_lines = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    diff_payload: dict[str, object] = {
        "unified_diff": "\n".join(diff_lines),
        "added_lines": added_lines,
        "removed_lines": removed_lines,
    }
    return backup1, backup2, diff_payload


def fetch_running_config(olt: OLTDevice) -> str | None:
    """Fetch a lightweight running-config snapshot via SNMP."""
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
    except ImportError:
        return None

    try:
        engine = SnmpEngine()
        community = CommunityData("public", mpModel=1)  # noqa: S508
        target = UdpTransportTarget((olt.mgmt_ip, 161), timeout=6, retries=0)
        oids = [
            "1.3.6.1.2.1.1.5.0",  # sysName
            "1.3.6.1.2.1.1.1.0",  # sysDescr
            "1.3.6.1.2.1.1.3.0",  # sysUpTime
        ]
        lines = [
            f"# OLT Config Snapshot: {olt.name}",
            f"# IP: {olt.mgmt_ip}",
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
        if len(lines) <= 4:
            return None
        return "\n".join(lines) + "\n"
    except Exception:
        return None


def test_olt_connection(db: Session, olt_id: str) -> tuple[bool, str]:
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"
    if not olt.mgmt_ip:
        return False, "Management IP is required"
    config = fetch_running_config(olt)
    if not config:
        return False, "Connection test failed: unable to fetch SNMP data"
    return True, "Connection test successful"


def run_test_backup(db: Session, olt_id: str) -> tuple[OltConfigBackup | None, str]:
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return None, "OLT not found"
    if not olt.mgmt_ip:
        return None, "Management IP is required"

    config_text = fetch_running_config(olt)
    if not config_text:
        return None, "Test backup failed: could not fetch running configuration"

    try:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        safe_name = olt.name.replace(" ", "_").replace("/", "_")[:60]
        filename = f"{safe_name}_{timestamp}.txt"
        base = _olt_backup_base_dir()
        olt_dir = base / str(olt.id)
        olt_dir.mkdir(parents=True, exist_ok=True)
        filepath = olt_dir / filename
        filepath.write_text(config_text)
        backup = OltConfigBackup(
            id=uuid.uuid4(),
            olt_device_id=olt.id,
            backup_type=OltConfigBackupType.manual,
            file_path=str(filepath.relative_to(base)),
            file_size_bytes=len(config_text.encode()),
        )
        db.add(backup)
        db.commit()
        db.refresh(backup)
        return backup, "Test backup completed successfully"
    except Exception as exc:
        db.rollback()
        return None, f"Test backup failed: {exc}"
