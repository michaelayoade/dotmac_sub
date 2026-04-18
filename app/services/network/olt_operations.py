"""Operational OLT helpers for backups, connectivity, CLI, and firmware."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from difflib import unified_diff
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OltConfigBackup, OltConfigBackupType, OLTDevice, OntUnit
from app.services.credential_crypto import decrypt_credential
from app.services.network import olt_ssh as olt_ssh_service
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.olt_monitoring_devices import find_linked_network_device
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.ont_status_adapter import (
    get_ont_status as get_adapter_status,
)
from app.services.network.serial_utils import (
    normalize as normalize_serial,
)
from app.services.network.serial_utils import (
    search_candidates as serial_search_candidates,
)

logger = logging.getLogger(__name__)

_FALLBACK_OLT_BACKUP_DIR = Path("uploads/olt_config_backups")

_CLI_ALLOWED_PREFIXES: list[str] = [
    "display ",
    "show ",
    "ping ",
    "traceroute ",
    "dir ",
    "list ",
]

_CLI_BLOCKED_PATTERNS: list[str] = [
    "reset",
    "reboot",
    "shutdown",
    "delete",
    "undo ",
    "save",
    "commit",
    "system-software",
    "startup",
    "format",
]


def _normalize_ont_status_serial(serial_number: str) -> tuple[str | None, str | None]:
    serial = str(serial_number or "").replace("-", "").strip().upper()
    if not serial:
        return None, "ONT serial number is required"
    if len(serial) > 64:
        return None, "ONT serial number is too long"
    if not re.fullmatch(r"[A-Z0-9]+", serial):
        return None, "ONT serial number may only contain letters, numbers, and dashes"
    return serial, None


def olt_backup_base_dir() -> Path:
    configured = os.getenv("OLT_BACKUP_DIR", "/app/uploads/olt_config_backups")
    candidate = Path(configured)
    if candidate.exists():
        return candidate
    return _FALLBACK_OLT_BACKUP_DIR


def resolve_backup_file(file_path: str) -> Path:
    base = olt_backup_base_dir().resolve()
    candidate = (base / file_path).resolve()
    if not str(candidate).startswith(str(base)):
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
    except (ValueError, TypeError) as exc:
        logger.warning("Invalid backup_id %s: %s", backup_id, exc)
        return None


def backup_file_path(backup: OltConfigBackup) -> Path:
    return resolve_backup_file(backup.file_path)


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
        raise HTTPException(
            status_code=400, detail="Backups must belong to the same OLT"
        )

    text1 = read_backup_content(backup1)
    text2 = read_backup_content(backup2)
    diff_lines = list(
        unified_diff(
            text1.splitlines(),
            text2.splitlines(),
            fromfile=str(backup1.file_path),
            tofile=str(backup2.file_path),
            lineterm="",
        )
    )
    added_lines = sum(
        1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
    )
    removed_lines = sum(
        1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
    )
    return (
        backup1,
        backup2,
        {
            "unified_diff": "\n".join(diff_lines),
            "added_lines": added_lines,
            "removed_lines": removed_lines,
        },
    )


def fetch_running_config(olt: OLTDevice, db: Session | None = None) -> str | None:
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

    community_str = "public"
    if db is not None:
        linked = find_linked_network_device(
            db, mgmt_ip=olt.mgmt_ip, hostname=olt.hostname, name=olt.name
        )
        if linked and linked.snmp_community:
            decrypted = decrypt_credential(linked.snmp_community)
            if decrypted:
                community_str = decrypted

    try:
        engine = SnmpEngine()
        community = CommunityData(community_str, mpModel=1)  # nosec  # noqa: S508
        target = UdpTransportTarget((olt.mgmt_ip, 161), timeout=6, retries=0)
        oids = [
            "1.3.6.1.2.1.1.5.0",
            "1.3.6.1.2.1.1.1.0",
            "1.3.6.1.2.1.1.3.0",
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
                lines.append(
                    f"{var_bind[0].prettyPrint()} = {var_bind[1].prettyPrint()}"
                )
        if len(lines) <= 4:
            return None
        return "\n".join(lines) + "\n"
    except Exception as exc:
        logger.warning("SNMP config fetch failed for OLT %s: %s", olt.name, exc)
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


def test_olt_snmp_connection(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[bool, str]:
    """Run an on-demand SNMP test for an OLT via its linked monitoring device."""
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"

    linked = find_linked_network_device(
        db,
        mgmt_ip=olt.mgmt_ip,
        hostname=olt.hostname,
        name=olt.name,
    )
    if not linked:
        return False, "No linked monitoring device found for this OLT"
    if not linked.snmp_enabled:
        return False, "SNMP is disabled on the linked monitoring device"

    try:
        from app.services import web_network_core_runtime as core_runtime_service

        device, error = core_runtime_service.snmp_check_device(db, str(linked.id))
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Manual SNMP test failed for OLT %s", olt_id)
        message = f"SNMP test failed: {exc!s}"
        log_olt_audit_event(
            db,
            request=request,
            action="test_snmp_connection",
            entity_id=olt_id,
            metadata={"result": "error", "message": message},
            status_code=500,
            is_success=False,
        )
        return False, message

    if error:
        ok, message = False, f"SNMP test failed: {error}"
    elif not device:
        ok, message = False, "SNMP test failed: linked device not found"
    elif device.last_snmp_ok:
        ok, message = True, "SNMP test successful"
    else:
        ok, message = False, "SNMP test failed: no response from device"
    log_olt_audit_event(
        db,
        request=request,
        action="test_snmp_connection",
        entity_id=olt_id,
        metadata={"result": "success" if ok else "error", "message": message},
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return ok, message


def extract_firmware_version(version_output: str) -> str | None:
    """Extract firmware version string from OLT CLI version output."""
    import re

    for pattern in [
        r"(?:software\s+version|version)\s*[:=]?\s*([^\s,()]+)",
        r"VRP\s+\(R\)\s+software,\s+Version\s+(\S+)",
        r"Version\s+(\S+)",
    ]:
        match = re.search(pattern, version_output, re.IGNORECASE)
        if match:
            return match.group(1).strip()[:120]
    return None


def test_olt_ssh_connection(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[bool, str, str | None]:
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        ok, message, policy_key = False, "OLT not found", None
        log_olt_audit_event(
            db,
            request=request,
            action="test_ssh_connection",
            entity_id=olt_id,
            metadata={
                "result": "error",
                "policy_key": policy_key,
                "message": message,
            },
            status_code=500,
            is_success=False,
        )
        return ok, message, policy_key
    ok, message, policy_key = olt_ssh_service.test_connection(olt)
    if ok and policy_key:
        try:
            _policy_key, version_output = olt_ssh_service.run_version_probe(olt)
            fw = extract_firmware_version(version_output)
            if fw and fw != olt.firmware_version:
                olt.firmware_version = fw
                db.commit()
        except Exception:
            logger.debug(
                "Firmware probe persistence failed for OLT %s",
                olt.id,
                exc_info=True,
            )
        message = f"{message} ({policy_key})"
        ok = True
    log_olt_audit_event(
        db,
        request=request,
        action="test_ssh_connection",
        entity_id=olt_id,
        metadata={
            "result": "success" if ok else "error",
            "policy_key": policy_key,
            "message": message,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return ok, message, policy_key


def test_olt_netconf_connection(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[bool, str, list[str]]:
    from app.services.network import olt_netconf

    olt = get_olt_or_none(db, olt_id)
    capabilities: list[str]
    if not olt:
        ok, message, capabilities = False, "OLT not found", []
    else:
        ok, message, capabilities = olt_netconf.test_connection(olt)
    log_olt_audit_event(
        db,
        request=request,
        action="test_netconf_connection",
        entity_id=olt_id,
        metadata={
            "result": "success" if ok else "error",
            "message": message,
            "capabilities_count": len(capabilities),
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return ok, message, capabilities


def get_olt_netconf_config(
    db: Session, olt_id: str, *, filter_xpath: str | None = None
) -> tuple[bool, str, str]:
    from app.services.network import olt_netconf

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", ""
    return olt_netconf.get_running_config(olt, filter_xpath=filter_xpath)


def get_olt_firmware_images(db: Session, olt_id: str) -> list:
    from app.models.network import OltFirmwareImage

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return []
    stmt = select(OltFirmwareImage).where(OltFirmwareImage.is_active.is_(True))
    if olt.vendor:
        stmt = stmt.where(OltFirmwareImage.vendor.ilike(f"%{olt.vendor}%"))
    return list(db.scalars(stmt.order_by(OltFirmwareImage.version.desc())).all())


def trigger_olt_firmware_upgrade(
    db: Session, olt_id: str, image_id: str, *, request: Request | None = None
) -> tuple[bool, str]:
    from app.models.network import OltFirmwareImage

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"
    image = db.get(OltFirmwareImage, image_id)
    if not image:
        return False, "Firmware image not found"
    if not image.is_active:
        return False, "Firmware image is not active"
    ok, message = olt_ssh_service.upgrade_firmware(
        olt, image.file_url, method=image.upgrade_method or "sftp"
    )
    log_olt_audit_event(
        db,
        request=request,
        action="firmware_upgrade",
        entity_id=olt_id,
        metadata={
            "result": "success" if ok else "error",
            "message": message,
            "firmware_image_id": image_id,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return ok, message


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
        base = olt_backup_base_dir()
        olt_dir = base / str(olt.id)
        olt_dir.mkdir(parents=True, exist_ok=True)
        filepath = olt_dir / filename
        filepath.write_text(config_text)
        config_bytes = config_text.encode()
        backup = OltConfigBackup(
            id=uuid.uuid4(),
            olt_device_id=olt.id,
            backup_type=OltConfigBackupType.manual,
            file_path=str(filepath.relative_to(base)),
            file_size_bytes=len(config_bytes),
            file_hash=hashlib.sha256(config_bytes).hexdigest(),
        )
        db.add(backup)
        db.commit()
        db.refresh(backup)
        return backup, "Test backup completed successfully"
    except Exception as exc:
        db.rollback()
        return None, f"Test backup failed: {exc}"


def validate_cli_command(command: str) -> str | None:
    """Check if a CLI command is safe to execute."""
    cmd = command.strip()
    if not cmd:
        return "Command is empty"
    if len(cmd) > 500:
        return "Command too long (max 500 characters)"
    if any(char in cmd for char in ("\r", "\n", "\x00")):
        return "Command must be a single line"

    cmd_lower = cmd.lower()
    for pattern in _CLI_BLOCKED_PATTERNS:
        if pattern in cmd_lower:
            return f"Command contains blocked keyword: {pattern}"

    if not any(cmd_lower.startswith(prefix) for prefix in _CLI_ALLOWED_PREFIXES):
        allowed = ", ".join(prefix.strip() for prefix in _CLI_ALLOWED_PREFIXES)
        return f"Only read-only commands allowed. Permitted prefixes: {allowed}"
    return None


def execute_cli_command(
    db: Session, olt_id: str, command: str, *, request: Request | None = None
) -> tuple[bool, str, str]:
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", ""

    error = validate_cli_command(command)
    if error:
        log_olt_audit_event(
            db,
            request=request,
            action="run_cli_command",
            entity_id=olt_id,
            metadata={"result": "error", "message": error, "command": command.strip()},
            status_code=400,
            is_success=False,
        )
        return False, error, ""

    ok, message, output = olt_ssh_service.run_cli_command(olt, command.strip())
    logger.info(
        "CLI command on OLT %s: %s -> %s",
        olt.name,
        command.strip(),
        "ok" if ok else "failed",
    )
    log_olt_audit_event(
        db,
        request=request,
        action="run_cli_command",
        entity_id=olt_id,
        metadata={
            "result": "success" if ok else "error",
            "message": message,
            "command": command.strip(),
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return ok, message, output


def fetch_running_config_ssh_preview(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[bool, str, str]:
    """Fetch full running config through SSH without storing a backup snapshot."""
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", ""

    ok, message, config_text = olt_ssh_service.fetch_running_config_ssh(olt)
    log_olt_audit_event(
        db,
        request=request,
        action="get_ssh_running_config",
        entity_id=olt_id,
        metadata={
            "result": "success" if ok else "error",
            "message": message,
            "bytes": len(config_text.encode()) if config_text else 0,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return ok, message, config_text


def get_ont_status_by_serial(
    db: Session,
    olt_id: str,
    serial_number: str,
    *,
    request: Request | None = None,
) -> tuple[bool, str, dict[str, object]]:
    """Lookup an ONT by serial on an OLT, then read its full OLT-side status."""
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", {}

    normalized_serial, error = _normalize_ont_status_serial(serial_number)
    if error or not normalized_serial:
        log_olt_audit_event(
            db,
            request=request,
            action="get_ont_status_by_serial",
            entity_id=olt_id,
            metadata={
                "result": "error",
                "message": error or "Invalid ONT serial number",
                "serial_number": str(serial_number or "").strip(),
            },
            status_code=400,
            is_success=False,
        )
        return False, error or "Invalid ONT serial number", {}

    from app.services.network import olt_ssh_ont as olt_ssh_ont_service

    lookup_serial = normalized_serial
    find_msg = ""
    found = None
    for candidate in serial_search_candidates(normalized_serial):
        candidate_serial, candidate_error = _normalize_ont_status_serial(candidate)
        if candidate_error or not candidate_serial:
            continue
        find_ok, find_msg, found = olt_ssh_ont_service.find_ont_by_serial(
            olt, candidate_serial
        )
        if not find_ok:
            log_olt_audit_event(
                db,
                request=request,
                action="get_ont_status_by_serial",
                entity_id=olt_id,
                metadata={
                    "result": "error",
                    "message": find_msg,
                    "serial_number": normalized_serial,
                    "lookup_serial": candidate_serial,
                },
                status_code=500,
                is_success=False,
            )
            return False, find_msg, {}
        if found is not None:
            lookup_serial = candidate_serial
            break

    if found is None:
        message = find_msg or f"ONT {normalized_serial} is not registered on {olt.name}"
        log_olt_audit_event(
            db,
            request=request,
            action="get_ont_status_by_serial",
            entity_id=olt_id,
            metadata={
                "result": "error",
                "message": message,
                "serial_number": normalized_serial,
                "lookup_serial": lookup_serial,
            },
            status_code=404,
            is_success=False,
        )
        return False, message, {}

    status_ok, status_msg, status = olt_ssh_ont_service.get_ont_status(
        olt, found.fsp, found.onu_id
    )
    if not status_ok or status is None:
        log_olt_audit_event(
            db,
            request=request,
            action="get_ont_status_by_serial",
            entity_id=olt_id,
            metadata={
                "result": "error",
                "message": status_msg,
                "serial_number": normalized_serial,
                "lookup_serial": lookup_serial,
                "fsp": found.fsp,
                "ont_id": found.onu_id,
            },
            status_code=500,
            is_success=False,
        )
        return False, status_msg, {}

    payload: dict[str, object] = {
        "requested_serial": normalized_serial,
        "lookup_serial": lookup_serial,
        "registered_serial": found.real_serial,
        "status_serial": status.serial_number,
        "fsp": found.fsp,
        "ont_id": found.onu_id,
        "run_state": status.run_state or found.run_state,
        "config_state": status.config_state,
        "match_state": status.match_state,
    }

    # Try to find matching ONT record and get unified status from adapter
    ont_record = _find_ont_by_serial_in_db(db, normalized_serial, olt.id)
    if ont_record:
        adapter_status = get_adapter_status(db, ont_record, include_optical=True)
        payload["effective_status"] = adapter_status.online_status.value
        payload["status_source"] = adapter_status.status_source.value
        payload["acs_status"] = adapter_status.acs_status.value
        if adapter_status.optical_metrics and adapter_status.optical_metrics.has_signal_data:
            metrics = adapter_status.optical_metrics
            payload["olt_rx_signal_dbm"] = metrics.olt_rx_dbm
            payload["onu_rx_signal_dbm"] = metrics.onu_rx_dbm
            payload["onu_tx_signal_dbm"] = metrics.onu_tx_dbm
            payload["optical_source"] = metrics.source

    message = (
        f"ONT {normalized_serial} is registered on {found.fsp} as ONT-ID "
        f"{found.onu_id} ({payload['run_state']})."
    )
    log_olt_audit_event(
        db,
        request=request,
        action="get_ont_status_by_serial",
        entity_id=olt_id,
        metadata={
            "result": "success",
            "message": message,
            **payload,
        },
        status_code=200,
        is_success=True,
    )
    return True, message, payload


def _find_ont_by_serial_in_db(
    db: Session, serial_number: str, olt_id: UUID
) -> OntUnit | None:
    """Find an ONT record in the database by serial number and OLT."""
    normalized = normalize_serial(serial_number)
    if not normalized:
        return None

    # Try exact match first
    stmt = select(OntUnit).where(
        OntUnit.olt_device_id == olt_id,
        OntUnit.is_active.is_(True),
    )
    onts = db.scalars(stmt).all()

    for ont in onts:
        ont_serial = normalize_serial(getattr(ont, "serial_number", None))
        if ont_serial and (ont_serial == normalized or normalized in ont_serial or ont_serial in normalized):
            return ont

    return None


def backup_running_config_ssh(
    db: Session, olt_id: str
) -> tuple[OltConfigBackup | None, str]:
    """Fetch full running config via SSH and save as backup."""
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return None, "OLT not found"

    ok, message, config_text = olt_ssh_service.fetch_running_config_ssh(olt)
    if not ok or not config_text:
        return None, f"SSH config backup failed: {message}"

    try:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        safe_name = olt.name.replace(" ", "_").replace("/", "_")[:60]
        filename = f"{safe_name}_ssh_{timestamp}.txt"
        base = olt_backup_base_dir()
        olt_dir = base / str(olt.id)
        olt_dir.mkdir(parents=True, exist_ok=True)
        filepath = olt_dir / filename
        filepath.write_text(config_text)
        config_bytes = config_text.encode()
        backup = OltConfigBackup(
            id=uuid.uuid4(),
            olt_device_id=olt.id,
            backup_type=OltConfigBackupType.manual,
            file_path=str(filepath.relative_to(base)),
            file_size_bytes=len(config_bytes),
            file_hash=hashlib.sha256(config_bytes).hexdigest(),
        )
        db.add(backup)
        db.commit()
        db.refresh(backup)
        logger.info("SSH config backup saved for OLT %s: %s", olt.name, filename)
        return backup, "Full running config backed up via SSH"
    except Exception as exc:
        db.rollback()
        return None, f"Failed to save SSH backup: {exc}"
