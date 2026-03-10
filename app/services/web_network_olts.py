"""Service helpers for admin OLT web routes."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from difflib import unified_diff
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network import OltConfigBackup, OltConfigBackupType, OLTDevice, OntUnit
from app.models.network_monitoring import DeviceRole, DeviceType, NetworkDevice
from app.schemas.network import OLTDeviceCreate, OLTDeviceUpdate
from app.services import network as network_service
from app.services.audit_helpers import (
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.credential_crypto import decrypt_credential, encrypt_credential
from app.services.network import olt_ssh as olt_ssh_service

logger = logging.getLogger(__name__)


def _encrypt_if_set(values: Mapping[str, Any], key: str) -> str | None:
    """Extract a string value from form data, encrypt if non-empty."""
    raw = str(values.get(key) or "").strip() or None
    if raw:
        return encrypt_credential(raw)
    return None
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
    ssh_port_raw = str(form.get("ssh_port", "")).strip()
    netconf_port_raw = str(form.get("netconf_port", "")).strip()
    snmp_port_raw = str(form.get("snmp_port", "")).strip()
    return {
        "name": form.get("name", "").strip(),
        "hostname": form.get("hostname", "").strip() or None,
        "mgmt_ip": form.get("mgmt_ip", "").strip() or None,
        "vendor": form.get("vendor", "").strip() or None,
        "model": form.get("model", "").strip() or None,
        "serial_number": form.get("serial_number", "").strip() or None,
        "ssh_username": form.get("ssh_username", "").strip() or None,
        "ssh_password": form.get("ssh_password", "").strip() or None,
        "ssh_port": int(ssh_port_raw)
        if ssh_port_raw.isdigit()
        else ssh_port_raw or None,
        "netconf_enabled": form.get("netconf_enabled") == "true",
        "netconf_port": int(netconf_port_raw)
        if netconf_port_raw.isdigit()
        else netconf_port_raw or None,
        "tr069_acs_server_id": form.get("tr069_acs_server_id", "").strip() or None,
        "snmp_enabled": form.get("snmp_enabled") == "true",
        "snmp_port": int(snmp_port_raw)
        if snmp_port_raw.isdigit()
        else snmp_port_raw or None,
        "snmp_version": form.get("snmp_version", "").strip() or "v2c",
        "snmp_community": form.get("snmp_community", "").strip() or None,
        "snmp_username": form.get("snmp_username", "").strip() or None,
        "snmp_auth_protocol": form.get("snmp_auth_protocol", "").strip() or None,
        "snmp_auth_secret": form.get("snmp_auth_secret", "").strip() or None,
        "snmp_priv_protocol": form.get("snmp_priv_protocol", "").strip() or None,
        "snmp_priv_secret": form.get("snmp_priv_secret", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
        "is_active": form.get("is_active") == "true",
    }


def validate_values(
    db: Session, values: dict[str, object], *, current_olt: OLTDevice | None = None
) -> str | None:
    """Validate required fields and uniqueness."""
    if not values.get("name"):
        return "Name is required"
    ssh_port = values.get("ssh_port")
    netconf_enabled = bool(values.get("netconf_enabled"))
    netconf_port = values.get("netconf_port")
    if ssh_port is not None and (
        not isinstance(ssh_port, int) or ssh_port < 1 or ssh_port > 65535
    ):
        return "SSH port must be between 1 and 65535"
    if netconf_port is not None and (
        not isinstance(netconf_port, int) or netconf_port < 1 or netconf_port > 65535
    ):
        return "NETCONF port must be between 1 and 65535"
    snmp_port = values.get("snmp_port")
    if snmp_port is not None and (
        not isinstance(snmp_port, int) or snmp_port < 1 or snmp_port > 65535
    ):
        return "SNMP port must be between 1 and 65535"
    if netconf_enabled and not values.get("ssh_username"):
        return "SSH username is required when NETCONF is enabled"
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
    ssh_password = values.get("ssh_password")
    encrypted_password = encrypt_credential(
        ssh_password if isinstance(ssh_password, str) else None
    )
    return OLTDeviceCreate.model_validate(
        {
            "name": values.get("name"),
            "hostname": values.get("hostname"),
            "mgmt_ip": values.get("mgmt_ip"),
            "vendor": values.get("vendor"),
            "model": values.get("model"),
            "serial_number": values.get("serial_number"),
            "ssh_username": values.get("ssh_username"),
            "ssh_password": encrypted_password,
            "ssh_port": values.get("ssh_port"),
            "netconf_enabled": values.get("netconf_enabled"),
            "netconf_port": values.get("netconf_port"),
            "tr069_acs_server_id": values.get("tr069_acs_server_id"),
            "notes": values.get("notes"),
            "is_active": values.get("is_active"),
        }
    )


def update_payload(values: dict[str, object]) -> OLTDeviceUpdate:
    """Build update payload from parsed values."""
    ssh_password = values.get("ssh_password")
    encrypted_password = encrypt_credential(
        ssh_password if isinstance(ssh_password, str) else None
    )
    return OLTDeviceUpdate.model_validate(
        {
            "name": values.get("name"),
            "hostname": values.get("hostname"),
            "mgmt_ip": values.get("mgmt_ip"),
            "vendor": values.get("vendor"),
            "model": values.get("model"),
            "serial_number": values.get("serial_number"),
            "ssh_username": values.get("ssh_username"),
            "ssh_password": encrypted_password,
            "ssh_port": values.get("ssh_port"),
            "netconf_enabled": values.get("netconf_enabled"),
            "netconf_port": values.get("netconf_port"),
            "tr069_acs_server_id": values.get("tr069_acs_server_id"),
            "notes": values.get("notes"),
            "is_active": values.get("is_active"),
        }
    )


def _find_linked_network_device(
    db: Session,
    *,
    mgmt_ip: str | None,
    hostname: str | None,
    name: str,
) -> NetworkDevice | None:
    if mgmt_ip:
        matched = db.scalars(
            select(NetworkDevice).where(NetworkDevice.mgmt_ip == mgmt_ip)
        ).first()
        if matched:
            return matched
    if hostname:
        matched = db.scalars(
            select(NetworkDevice).where(NetworkDevice.hostname == hostname)
        ).first()
        if matched:
            return matched
    return db.scalars(select(NetworkDevice).where(NetworkDevice.name == name)).first()


def sync_monitoring_device(
    db: Session, olt: OLTDevice, values: dict[str, object]
) -> None:
    """Sync OLT form SNMP fields into linked Core Device record."""
    mgmt_ip = str(values.get("mgmt_ip") or olt.mgmt_ip or "").strip() or None
    hostname = str(values.get("hostname") or olt.hostname or "").strip() or None
    name = str(values.get("name") or olt.name or "").strip() or olt.name
    linked = _find_linked_network_device(
        db,
        mgmt_ip=mgmt_ip,
        hostname=hostname,
        name=name,
    )

    if linked is None:
        linked = NetworkDevice(
            name=name,
            hostname=hostname,
            mgmt_ip=mgmt_ip,
            vendor=str(values.get("vendor") or olt.vendor or "").strip() or None,
            model=str(values.get("model") or olt.model or "").strip() or None,
            serial_number=str(
                values.get("serial_number") or olt.serial_number or ""
            ).strip()
            or None,
            role=DeviceRole.edge,
            device_type=DeviceType.router,
            snmp_enabled=bool(values.get("snmp_enabled")),
            snmp_port=values.get("snmp_port")
            if isinstance(values.get("snmp_port"), int)
            else 161,
            snmp_version=str(values.get("snmp_version") or "v2c"),
            snmp_community=_encrypt_if_set(values, "snmp_community"),
            snmp_username=str(values.get("snmp_username") or "").strip() or None,
            snmp_auth_protocol=str(values.get("snmp_auth_protocol") or "").strip()
            or None,
            snmp_auth_secret=_encrypt_if_set(values, "snmp_auth_secret"),
            snmp_priv_protocol=str(values.get("snmp_priv_protocol") or "").strip()
            or None,
            snmp_priv_secret=_encrypt_if_set(values, "snmp_priv_secret"),
            is_active=bool(values.get("is_active")),
        )
        db.add(linked)
        db.commit()
        return

    linked.name = name
    linked.hostname = hostname
    linked.mgmt_ip = mgmt_ip
    linked.vendor = str(values.get("vendor") or olt.vendor or "").strip() or None
    linked.model = str(values.get("model") or olt.model or "").strip() or None
    linked.serial_number = (
        str(values.get("serial_number") or olt.serial_number or "").strip() or None
    )
    linked.snmp_enabled = bool(values.get("snmp_enabled"))
    linked.snmp_port = (
        values.get("snmp_port") if isinstance(values.get("snmp_port"), int) else 161
    )
    linked.snmp_version = str(values.get("snmp_version") or "v2c")
    linked.snmp_community = _encrypt_if_set(values, "snmp_community")
    linked.snmp_username = str(values.get("snmp_username") or "").strip() or None
    linked.snmp_auth_protocol = (
        str(values.get("snmp_auth_protocol") or "").strip() or None
    )
    linked.snmp_auth_secret = _encrypt_if_set(values, "snmp_auth_secret")
    linked.snmp_priv_protocol = (
        str(values.get("snmp_priv_protocol") or "").strip() or None
    )
    linked.snmp_priv_secret = _encrypt_if_set(values, "snmp_priv_secret")
    linked.is_active = bool(values.get("is_active"))
    db.commit()


def build_form_model(db: Session, olt: OLTDevice) -> SimpleNamespace:
    """Build OLT form data enriched with linked core-device SNMP fields."""
    linked = _find_linked_network_device(
        db,
        mgmt_ip=olt.mgmt_ip,
        hostname=olt.hostname,
        name=olt.name,
    )
    return SimpleNamespace(
        id=olt.id,
        name=olt.name,
        hostname=olt.hostname,
        mgmt_ip=olt.mgmt_ip,
        vendor=olt.vendor,
        model=olt.model,
        serial_number=olt.serial_number,
        ssh_username=olt.ssh_username,
        ssh_password="",
        ssh_port=olt.ssh_port,
        netconf_enabled=olt.netconf_enabled,
        netconf_port=olt.netconf_port,
        tr069_acs_server_id=olt.tr069_acs_server_id,
        notes=olt.notes,
        is_active=olt.is_active,
        snmp_enabled=bool(getattr(linked, "snmp_enabled", False)),
        snmp_port=getattr(linked, "snmp_port", 161),
        snmp_version=getattr(linked, "snmp_version", "v2c"),
        snmp_community=decrypt_credential(v) if (v := getattr(linked, "snmp_community", None)) else None,
        snmp_username=getattr(linked, "snmp_username", None),
        snmp_auth_protocol=getattr(linked, "snmp_auth_protocol", None),
        snmp_auth_secret="",
        snmp_priv_protocol=getattr(linked, "snmp_priv_protocol", None),
        snmp_priv_secret="",
    )


def create_olt(
    db: Session, values: dict[str, object]
) -> tuple[OLTDevice | None, str | None]:
    """Create OLT and normalize integrity errors."""
    try:
        olt = network_service.olt_devices.create(db=db, payload=create_payload(values))
        sync_monitoring_device(db, olt, values)
        return olt, None
    except IntegrityError as exc:
        logger.warning("OLT create integrity error: %s", exc)
        db.rollback()
        return None, integrity_error_message(exc)


def _queue_acs_propagation(db: Session, olt: OLTDevice) -> None:
    """Push ACS ManagementServer parameters to all active ONTs under an OLT."""
    from app.models.tr069 import Tr069AcsServer
    from app.services.credential_crypto import decrypt_credential

    if not olt.tr069_acs_server_id:
        return
    server = db.get(Tr069AcsServer, str(olt.tr069_acs_server_id))
    if not server or not server.cwmp_url:
        return

    onts = (
        db.query(OntUnit)
        .filter(OntUnit.olt_device_id == olt.id)
        .filter(OntUnit.is_active.is_(True))
        .all()
    )
    if not onts:
        return

    acs_params: dict[str, str] = {
        "Device.ManagementServer.URL": server.cwmp_url,
        "Device.ManagementServer.PeriodicInformEnable": "true",
        "Device.ManagementServer.PeriodicInformInterval": "3600",
    }
    if server.cwmp_username:
        acs_params["Device.ManagementServer.Username"] = server.cwmp_username
    if server.cwmp_password:
        password = decrypt_credential(server.cwmp_password)
        if password:
            acs_params["Device.ManagementServer.Password"] = password

    from app.services.network._resolve import resolve_genieacs

    for ont in onts:
        try:
            resolved = resolve_genieacs(db, ont)
            if resolved:
                client, device_id = resolved
                client.set_parameter_values(device_id, acs_params)
                logger.info("Propagated ACS config to ONT %s", ont.serial_number)
        except Exception as exc:
            logger.error(
                "Failed to propagate ACS to ONT %s: %s", ont.serial_number, exc
            )


def update_olt(
    db: Session, olt_id: str, values: dict[str, object]
) -> tuple[OLTDevice | None, str | None]:
    """Update OLT and normalize integrity errors."""
    try:
        current = network_service.olt_devices.get(db=db, device_id=olt_id)
        old_acs_id = (
            str(current.tr069_acs_server_id) if current.tr069_acs_server_id else None
        )
        payload_values = dict(values)
        if payload_values.get("ssh_password") is None:
            payload_values["ssh_password"] = current.ssh_password
        olt = network_service.olt_devices.update(
            db=db,
            device_id=olt_id,
            payload=update_payload(payload_values),
        )
        sync_monitoring_device(db, olt, payload_values)
        new_acs_id = str(olt.tr069_acs_server_id) if olt.tr069_acs_server_id else None
        if old_acs_id != new_acs_id and new_acs_id:
            _queue_acs_propagation(db, olt)
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
        raise HTTPException(
            status_code=400, detail="Backups must belong to the same OLT"
        )

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
    added_lines = sum(
        1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
    )
    removed_lines = sum(
        1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
    )
    diff_payload: dict[str, object] = {
        "unified_diff": "\n".join(diff_lines),
        "added_lines": added_lines,
        "removed_lines": removed_lines,
    }
    return backup1, backup2, diff_payload


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

    # Resolve SNMP community from linked device, falling back to "public"
    community_str = "public"
    if db is not None:
        linked = _find_linked_network_device(
            db, mgmt_ip=olt.mgmt_ip, hostname=olt.hostname, name=olt.name
        )
        if linked and linked.snmp_community:
            community_str = decrypt_credential(linked.snmp_community)

    try:
        engine = SnmpEngine()
        community = CommunityData(community_str, mpModel=1)  # noqa: S508
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


def test_olt_snmp_connection(db: Session, olt_id: str) -> tuple[bool, str]:
    """Run an on-demand SNMP test for an OLT via its linked monitoring device."""
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"

    linked = _find_linked_network_device(
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


def test_olt_ssh_connection(db: Session, olt_id: str) -> tuple[bool, str, str | None]:
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", None
    ok, message, policy_key = olt_ssh_service.test_connection(olt)
    if ok and policy_key:
        return True, f"{message} ({policy_key})", policy_key
    return ok, message, policy_key


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
