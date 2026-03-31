"""Operational OLT helpers for backups, connectivity, CLI, and firmware."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from difflib import unified_diff
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OltConfigBackup, OltConfigBackupType, OLTDevice

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
    "config",
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
    return backup1, backup2, {
        "unified_diff": "\n".join(diff_lines),
        "added_lines": added_lines,
        "removed_lines": removed_lines,
    }


def fetch_running_config(olt: OLTDevice, db: Session | None = None) -> str | None:
    """Fetch a lightweight running-config snapshot via SNMP."""
    from app.services import web_network_olts as web_network_olts_service

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
        linked = web_network_olts_service._find_linked_network_device(
            db, mgmt_ip=olt.mgmt_ip, hostname=olt.hostname, name=olt.name
        )
        if linked and linked.snmp_community:
            decrypted = web_network_olts_service.decrypt_credential(
                linked.snmp_community
            )
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
    from app.services import web_network_olts as web_network_olts_service

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"
    if not olt.mgmt_ip:
        return False, "Management IP is required"
    config = web_network_olts_service.fetch_running_config(olt)
    if not config:
        return False, "Connection test failed: unable to fetch SNMP data"
    return True, "Connection test successful"


def test_olt_snmp_connection(db: Session, olt_id: str) -> tuple[bool, str]:
    """Run an on-demand SNMP test for an OLT via its linked monitoring device."""
    from app.services import web_network_olts as web_network_olts_service

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"

    linked = web_network_olts_service._find_linked_network_device(
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
        return False, f"SNMP test failed: {exc!s}"

    if error:
        return False, f"SNMP test failed: {error}"
    if not device:
        return False, "SNMP test failed: linked device not found"
    if device.last_snmp_ok:
        return True, "SNMP test successful"
    return False, "SNMP test failed: no response from device"


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


def test_olt_ssh_connection(db: Session, olt_id: str) -> tuple[bool, str, str | None]:
    from app.services import web_network_olts as web_network_olts_service

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", None
    ok, message, policy_key = web_network_olts_service.olt_ssh_service.test_connection(
        olt
    )
    if ok and policy_key:
        try:
            _policy_key, version_output = (
                web_network_olts_service.olt_ssh_service.run_version_probe(olt)
            )
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
        return True, f"{message} ({policy_key})", policy_key
    return ok, message, policy_key


def test_olt_netconf_connection(
    db: Session, olt_id: str
) -> tuple[bool, str, list[str]]:
    from app.services import web_network_olts as web_network_olts_service
    from app.services.network import olt_netconf

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", []
    return olt_netconf.test_connection(olt)


def get_olt_netconf_config(
    db: Session, olt_id: str, *, filter_xpath: str | None = None
) -> tuple[bool, str, str]:
    from app.services import web_network_olts as web_network_olts_service
    from app.services.network import olt_netconf

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", ""
    return olt_netconf.get_running_config(olt, filter_xpath=filter_xpath)


def get_olt_firmware_images(db: Session, olt_id: str) -> list:
    from app.models.network import OltFirmwareImage
    from app.services import web_network_olts as web_network_olts_service

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return []
    stmt = select(OltFirmwareImage).where(OltFirmwareImage.is_active.is_(True))
    if olt.vendor:
        stmt = stmt.where(OltFirmwareImage.vendor.ilike(f"%{olt.vendor}%"))
    return list(db.scalars(stmt.order_by(OltFirmwareImage.version.desc())).all())


def trigger_olt_firmware_upgrade(
    db: Session, olt_id: str, image_id: str
) -> tuple[bool, str]:
    from app.models.network import OltFirmwareImage
    from app.services import web_network_olts as web_network_olts_service

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"
    image = db.get(OltFirmwareImage, image_id)
    if not image:
        return False, "Firmware image not found"
    if not image.is_active:
        return False, "Firmware image is not active"
    return web_network_olts_service.olt_ssh_service.upgrade_firmware(
        olt, image.file_url, method=image.upgrade_method or "sftp"
    )


def run_test_backup(db: Session, olt_id: str) -> tuple[OltConfigBackup | None, str]:
    from app.services import web_network_olts as web_network_olts_service

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return None, "OLT not found"
    if not olt.mgmt_ip:
        return None, "Management IP is required"

    config_text = web_network_olts_service.fetch_running_config(olt)
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


def validate_cli_command(command: str) -> str | None:
    """Check if a CLI command is safe to execute."""
    cmd = command.strip()
    if not cmd:
        return "Command is empty"
    if len(cmd) > 500:
        return "Command too long (max 500 characters)"

    cmd_lower = cmd.lower()
    for pattern in _CLI_BLOCKED_PATTERNS:
        if pattern in cmd_lower:
            return f"Command contains blocked keyword: {pattern}"

    if not any(cmd_lower.startswith(prefix) for prefix in _CLI_ALLOWED_PREFIXES):
        allowed = ", ".join(prefix.strip() for prefix in _CLI_ALLOWED_PREFIXES)
        return f"Only read-only commands allowed. Permitted prefixes: {allowed}"
    return None


def execute_cli_command(
    db: Session, olt_id: str, command: str
) -> tuple[bool, str, str]:
    from app.services import web_network_olts as web_network_olts_service

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", ""

    error = validate_cli_command(command)
    if error:
        return False, error, ""

    ok, message, output = web_network_olts_service.olt_ssh_service.run_cli_command(
        olt, command.strip()
    )
    logger.info(
        "CLI command on OLT %s: %s -> %s",
        olt.name,
        command.strip(),
        "ok" if ok else "failed",
    )
    return ok, message, output


def backup_running_config_ssh(
    db: Session, olt_id: str
) -> tuple[OltConfigBackup | None, str]:
    """Fetch full running config via SSH and save as backup."""
    from app.services import web_network_olts as web_network_olts_service

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return None, "OLT not found"

    ok, message, config_text = (
        web_network_olts_service.olt_ssh_service.fetch_running_config_ssh(olt)
    )
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
        logger.info("SSH config backup saved for OLT %s: %s", olt.name, filename)
        return backup, "Full running config backed up via SSH"
    except Exception as exc:
        db.rollback()
        return None, f"Failed to save SSH backup: {exc}"
