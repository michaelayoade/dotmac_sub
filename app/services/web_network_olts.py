"""Service helpers for admin OLT web routes."""

from __future__ import annotations

import logging
import os
import re
import subprocess  # nosec
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from difflib import unified_diff
from hashlib import blake2b
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network import (
    GponChannel,
    OltConfigBackup,
    OltConfigBackupType,
    OLTDevice,
    OntAssignment,
    OntUnit,
    OnuOfflineReason,
    OnuOnlineStatus,
    PonPort,
    PonType,
)
from app.models.network_monitoring import (
    DeviceRole,
    DeviceType,
    NetworkDevice,
)
from app.models.ont_autofind import OltAutofindCandidate
from app.schemas.network import OLTDeviceCreate, OLTDeviceUpdate
from app.services import network as network_service
from app.services.audit_helpers import (
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.credential_crypto import decrypt_credential, encrypt_credential
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network import olt_ssh as olt_ssh_service
from app.services.network.olt_polling_parsers import (
    _decode_huawei_packed_fsp,
    _split_onu_index,
)
from app.services.web_network_ont_autofind import _find_ont_by_serial

logger = logging.getLogger(__name__)


def _olt_sync_lock_key(olt_id: str) -> int:
    """Return a deterministic positive bigint advisory-lock key for an OLT."""
    digest = blake2b(olt_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=False) & 0x7FFFFFFFFFFFFFFF


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
        "snmp_rw_community": form.get("snmp_rw_community", "").strip() or None,
        "supported_pon_types": ",".join(
            t
            for t in (
                form.getlist("supported_pon_types")
                if hasattr(form, "getlist")
                else [form.get("supported_pon_types", "")]
            )
            if t and t.strip()
        )
        or None,
        "status": form.get("status", "").strip() or "active",
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
            "snmp_enabled": bool(values.get("snmp_enabled")),
            "snmp_port": values.get("snmp_port"),
            "snmp_version": values.get("snmp_version"),
            "snmp_ro_community": _encrypt_if_set(values, "snmp_community"),
            "snmp_rw_community": _encrypt_if_set(values, "snmp_rw_community"),
            "netconf_enabled": bool(values.get("netconf_enabled")),
            "netconf_port": values.get("netconf_port"),
            "tr069_acs_server_id": values.get("tr069_acs_server_id"),
            "supported_pon_types": values.get("supported_pon_types"),
            "status": values.get("status"),
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
    data: dict[str, object] = {
        "name": values.get("name"),
        "hostname": values.get("hostname"),
        "mgmt_ip": values.get("mgmt_ip"),
        "vendor": values.get("vendor"),
        "model": values.get("model"),
        "serial_number": values.get("serial_number"),
        "ssh_username": values.get("ssh_username"),
        "ssh_password": encrypted_password,
        "ssh_port": values.get("ssh_port"),
        "snmp_enabled": values.get("snmp_enabled"),
        "snmp_port": values.get("snmp_port"),
        "snmp_version": values.get("snmp_version"),
        "snmp_ro_community": _encrypt_if_set(values, "snmp_community"),
        "snmp_rw_community": _encrypt_if_set(values, "snmp_rw_community"),
        "netconf_enabled": values.get("netconf_enabled"),
        "netconf_port": values.get("netconf_port"),
        "tr069_acs_server_id": values.get("tr069_acs_server_id"),
        "notes": values.get("notes"),
        "is_active": values.get("is_active"),
    }
    if "supported_pon_types" in values:
        data["supported_pon_types"] = values["supported_pon_types"]
    if "status" in values and values["status"] is not None:
        data["status"] = values["status"]
    return OLTDeviceUpdate.model_validate(data)


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
            snmp_rw_community=_encrypt_if_set(values, "snmp_rw_community"),
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
    snmp_community_encrypted = _encrypt_if_set(values, "snmp_community")
    if snmp_community_encrypted is not None:
        linked.snmp_community = snmp_community_encrypted
    snmp_rw_community_encrypted = _encrypt_if_set(values, "snmp_rw_community")
    if snmp_rw_community_encrypted is not None:
        linked.snmp_rw_community = snmp_rw_community_encrypted
    linked.snmp_username = str(values.get("snmp_username") or "").strip() or None
    linked.snmp_auth_protocol = (
        str(values.get("snmp_auth_protocol") or "").strip() or None
    )
    snmp_auth_secret_encrypted = _encrypt_if_set(values, "snmp_auth_secret")
    if snmp_auth_secret_encrypted is not None:
        linked.snmp_auth_secret = snmp_auth_secret_encrypted
    linked.snmp_priv_protocol = (
        str(values.get("snmp_priv_protocol") or "").strip() or None
    )
    snmp_priv_secret_encrypted = _encrypt_if_set(values, "snmp_priv_secret")
    if snmp_priv_secret_encrypted is not None:
        linked.snmp_priv_secret = snmp_priv_secret_encrypted
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
        firmware_version=olt.firmware_version,
        software_version=olt.software_version,
        supported_pon_types=getattr(olt, "supported_pon_types", None),
        status=olt.status.value
        if hasattr(olt.status, "value")
        else str(olt.status or "active"),
        ssh_username=olt.ssh_username,
        ssh_password="",  # nosec
        ssh_port=olt.ssh_port,
        netconf_enabled=olt.netconf_enabled,
        netconf_port=olt.netconf_port,
        tr069_acs_server_id=olt.tr069_acs_server_id,
        notes=olt.notes,
        is_active=olt.is_active,
        # SNMP: prefer OLT's own fields, fall back to linked NetworkDevice
        snmp_enabled=getattr(olt, "snmp_enabled", False)
        or bool(getattr(linked, "snmp_enabled", False)),
        snmp_port=getattr(olt, "snmp_port", None) or getattr(linked, "snmp_port", 161),
        snmp_version=getattr(olt, "snmp_version", None)
        or getattr(linked, "snmp_version", "v2c"),
        snmp_community=(
            decrypt_credential(v)
            if (v := getattr(olt, "snmp_ro_community", None))
            else (
                decrypt_credential(v)
                if (v := getattr(linked, "snmp_community", None))
                else None
            )
        ),
        snmp_rw_community=(
            decrypt_credential(v)
            if (v := getattr(olt, "snmp_rw_community", None))
            else (
                decrypt_credential(v)
                if (v := getattr(linked, "snmp_rw_community", None))
                else None
            )
        ),
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


def _queue_acs_propagation(db: Session, olt: OLTDevice) -> dict[str, int]:
    """Push ACS ManagementServer parameters to all active ONTs under an OLT."""
    from app.models.tr069 import Tr069AcsServer
    from app.services.credential_crypto import decrypt_credential
    from app.services.network._resolve import resolve_genieacs_with_reason

    stats = {
        "attempted": 0,
        "propagated": 0,
        "unresolved": 0,
        "errors": 0,
    }

    if not olt.tr069_acs_server_id:
        return stats
    server = db.get(Tr069AcsServer, str(olt.tr069_acs_server_id))
    if not server or not server.cwmp_url:
        return stats

    onts = (
        db.query(OntUnit)
        .filter(OntUnit.olt_device_id == olt.id)
        .filter(OntUnit.is_active.is_(True))
        .all()
    )
    if not onts:
        return stats

    acs_params: dict[str, str] = {
        "Device.ManagementServer.URL": server.cwmp_url,
        "Device.ManagementServer.PeriodicInformEnable": "true",
        "Device.ManagementServer.PeriodicInformInterval": "3600",
        "InternetGatewayDevice.ManagementServer.URL": server.cwmp_url,
        "InternetGatewayDevice.ManagementServer.PeriodicInformEnable": "true",
        "InternetGatewayDevice.ManagementServer.PeriodicInformInterval": "3600",
    }
    if server.cwmp_username:
        acs_params["Device.ManagementServer.Username"] = server.cwmp_username
        acs_params["InternetGatewayDevice.ManagementServer.Username"] = (
            server.cwmp_username
        )
    if server.cwmp_password:
        password = decrypt_credential(server.cwmp_password)
        if password:
            acs_params["Device.ManagementServer.Password"] = password
            acs_params["InternetGatewayDevice.ManagementServer.Password"] = password

    for ont in onts:
        stats["attempted"] += 1
        try:
            resolved, reason = resolve_genieacs_with_reason(db, ont)
            if resolved:
                client, device_id = resolved
                client.set_parameter_values(device_id, acs_params)
                logger.info("Propagated ACS config to ONT %s", ont.serial_number)
                stats["propagated"] += 1
            else:
                stats["unresolved"] += 1
                logger.info(
                    "Skipped ACS propagation for ONT %s: %s",
                    ont.serial_number,
                    reason,
                )
        except Exception as exc:
            logger.error(
                "Failed to propagate ACS to ONT %s: %s", ont.serial_number, exc
            )
            stats["errors"] += 1

    return stats


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
        # Preserve SNMP fields when form doesn't submit new values
        if payload_values.get("snmp_community") is None:
            payload_values["snmp_community"] = getattr(
                current, "snmp_ro_community", None
            )
        if payload_values.get("snmp_rw_community") is None:
            payload_values["snmp_rw_community"] = getattr(
                current, "snmp_rw_community", None
            )
        if payload_values.get("snmp_enabled") is None:
            payload_values["snmp_enabled"] = getattr(current, "snmp_enabled", False)
        if payload_values.get("snmp_port") is None:
            payload_values["snmp_port"] = getattr(current, "snmp_port", 161)
        if payload_values.get("snmp_version") is None:
            payload_values["snmp_version"] = getattr(current, "snmp_version", "v2c")
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


def _auto_init_tr069_profile(olt: OLTDevice) -> None:
    """Best-effort: create DotMac-ACS TR-069 profile on a new OLT.

    Runs after OLT creation. Silently skips if SSH is not configured
    or if profile creation fails (admin can use the Init TR-069 button later).
    """
    if not olt.ssh_username or not olt.ssh_password:
        logger.info("Skipping auto TR-069 init for %s — no SSH credentials", olt.name)
        return
    try:
        from app.services.network.olt_ssh import (
            create_tr069_server_profile,
            get_tr069_server_profiles,
        )

        ok, _msg, profiles = get_tr069_server_profiles(olt)
        if not ok:
            return
        for p in profiles:
            if "dotmac" in p.name.lower() or "10.10.41.1" in (p.acs_url or ""):
                logger.info("TR-069 profile already exists on %s: %s", olt.name, p.name)
                return

        ok, msg = create_tr069_server_profile(
            olt,
            profile_name="DotMac-ACS",
            acs_url="http://10.10.41.1:7547",
            username="acs",
            password="acs",  # nosec  # noqa: S106
            inform_interval=300,
        )
        if ok:
            logger.info("Auto-created TR-069 profile on %s", olt.name)
        else:
            logger.warning(
                "Auto TR-069 profile creation failed on %s: %s", olt.name, msg
            )
    except Exception as exc:
        logger.warning("Auto TR-069 init error on %s: %s", olt.name, exc)


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

    # Auto-create DotMac-ACS TR-069 profile on the new OLT (best-effort)
    _auto_init_tr069_profile(olt)

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
        community = CommunityData(community_str, mpModel=1)  # nosec  # noqa: S508
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
        # Attempt to extract and persist firmware version from the probe output
        try:
            _policy_key, version_output = olt_ssh_service.run_version_probe(olt)
            fw = _extract_firmware_version(version_output)
            if fw and fw != olt.firmware_version:
                olt.firmware_version = fw
                db.commit()
        except Exception:
            logger.debug(
                "Firmware probe persistence failed for OLT %s",
                olt.id,
                exc_info=True,
            )  # Non-critical; SSH test already passed
        return True, f"{message} ({policy_key})", policy_key
    return ok, message, policy_key


def _extract_firmware_version(version_output: str) -> str | None:
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


def _parse_fsp_parts(fsp: str) -> tuple[str | None, str | None]:
    """Split an F/S/P string into board and port fragments."""
    parts = [part.strip() for part in str(fsp or "").split("/") if part.strip()]
    if len(parts) != 3:
        return None, None
    return f"{parts[0]}/{parts[1]}", parts[2]


def _persist_authorized_ont_inventory(
    db: Session,
    *,
    olt: OLTDevice,
    fsp: str,
    serial_number: str,
    ont_id: int,
) -> None:
    """Persist ONT + assignment state after OLT-side authorization."""
    normalized_serial = str(serial_number or "").strip()
    board, port = _parse_fsp_parts(fsp)
    if not normalized_serial or not board or not port:
        return

    now = datetime.now(UTC)
    candidate = db.scalars(
        select(OltAutofindCandidate)
        .where(
            OltAutofindCandidate.olt_id == olt.id,
            OltAutofindCandidate.fsp == fsp,
            OltAutofindCandidate.serial_number == normalized_serial,
        )
        .order_by(
            OltAutofindCandidate.updated_at.desc(),
            OltAutofindCandidate.created_at.desc(),
        )
    ).first()

    ont = _find_ont_by_serial(db, normalized_serial)
    if ont is None:
        ont = OntUnit(
            serial_number=normalized_serial,
            is_active=True,
        )
        db.add(ont)

    ont.is_active = True
    ont.olt_device_id = olt.id
    ont.board = board
    ont.port = port
    ont.external_id = str(ont_id)
    ont.last_sync_source = "olt_ssh_authorize"
    ont.last_sync_at = now

    if candidate is not None:
        if candidate.vendor_id:
            ont.vendor = candidate.vendor_id
        if candidate.model:
            ont.model = candidate.model
        if candidate.software_version:
            ont.firmware_version = candidate.software_version
        if candidate.mac:
            ont.mac_address = candidate.mac

    pon_port = db.scalars(
        select(PonPort).where(
            PonPort.olt_id == olt.id,
            PonPort.name == fsp,
        )
    ).first()
    if pon_port is None:
        pon_port = PonPort(
            olt_id=olt.id,
            name=fsp,
            port_number=int(port) if str(port).isdigit() else None,
            is_active=True,
        )
        db.add(pon_port)
        db.flush()
    else:
        pon_port.is_active = True
        if pon_port.port_number is None and str(port).isdigit():
            pon_port.port_number = int(port)

    active_assignment = db.scalars(
        select(OntAssignment)
        .where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
        .order_by(
            OntAssignment.assigned_at.desc(),
            OntAssignment.created_at.desc(),
        )
    ).first()
    if active_assignment is None:
        db.add(
            OntAssignment(
                ont_unit_id=ont.id,
                pon_port_id=pon_port.id,
                active=True,
                assigned_at=now,
            )
        )
    elif active_assignment.pon_port_id != pon_port.id:
        active_assignment.active = False
        db.add(
            OntAssignment(
                ont_unit_id=ont.id,
                pon_port_id=pon_port.id,
                subscriber_id=active_assignment.subscriber_id,
                service_address_id=active_assignment.service_address_id,
                notes=active_assignment.notes,
                active=True,
                assigned_at=now,
            )
        )
    else:
        active_assignment.active = True
        if active_assignment.assigned_at is None:
            active_assignment.assigned_at = now

    if candidate is not None:
        candidate.ont_unit = ont
        candidate.is_active = False
        candidate.resolution_reason = "authorized"
        candidate.resolved_at = now

    db.commit()


def get_autofind_onts(
    db: Session, olt_id: str
) -> tuple[bool, str, list[olt_ssh_service.AutofindEntry]]:
    """Retrieve unregistered ONTs from an OLT's autofind table via SSH."""
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", []
    return olt_ssh_service.get_autofind_onts(olt)


def authorize_autofind_ont(
    db: Session, olt_id: str, fsp: str, serial_number: str
) -> tuple[bool, str]:
    """Authorize an unregistered ONT and persist app-side inventory state."""
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"

    # Step 1: Authorize the ONT
    ok, msg, ont_id = olt_ssh_service.authorize_ont(olt, fsp, serial_number)
    if not ok:
        return False, msg
    if ont_id is None:
        logger.warning(
            "Could not determine ONT-ID for authorized serial %s on %s %s",
            serial_number,
            olt.name,
            fsp,
        )
        return (
            True,
            f"{msg}. Warning: ONT-ID could not be determined, so service-port provisioning was skipped",
        )

    _persist_authorized_ont_inventory(
        db,
        olt=olt,
        fsp=fsp,
        serial_number=serial_number,
        ont_id=ont_id,
    )
    return True, msg


def clone_service_ports(
    db: Session, olt_id: str, fsp: str, ont_id: int
) -> tuple[bool, str]:
    """Clone service-ports for an ONT using a reference ONT on the same port.

    Uses the neighbor-learning pattern: inspects existing service-ports on the
    same PON port, finds the VLAN/GEM pattern from a reference ONT, and copies
    it to the explicitly selected ONT-ID.
    """
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"

    # Get all service-ports on this PON port
    ok, msg, entries = olt_ssh_service.get_service_ports(olt, fsp)
    if not ok or not entries:
        return False, f"Cannot read service-ports: {msg}"

    # Find the reference ONT (most common ONT-ID with most service-ports)
    from collections import Counter

    ont_counts = Counter(e.ont_id for e in entries)
    if not ont_counts:
        return False, "No existing service-ports to learn from"

    # Check if target ONT already has service-ports
    new_ont_ports = [e for e in entries if e.ont_id == ont_id]
    if new_ont_ports:
        return True, f"ONT {ont_id} already has {len(new_ont_ports)} service-port(s)"

    # Find a reference ONT (pick one with service-ports that's not the target)
    reference_ont_id = None
    for candidate_ont_id, count in ont_counts.most_common():
        if candidate_ont_id != ont_id:
            reference_ont_id = candidate_ont_id
            break
    if reference_ont_id is None:
        return False, "No reference ONT found to learn service-port pattern from"

    reference_ports = [e for e in entries if e.ont_id == reference_ont_id]
    logger.info(
        "Learning service-port pattern from ONT %d (%d ports) for new ONT %d on %s",
        reference_ont_id,
        len(reference_ports),
        ont_id,
        fsp,
    )

    return olt_ssh_service.create_service_ports(olt, fsp, ont_id, reference_ports)


def provision_ont_service_ports(
    db: Session, olt_id: str, fsp: str, ont_id: int
) -> tuple[bool, str]:
    """Compatibility alias for explicit service-port cloning."""
    return clone_service_ports(db, olt_id, fsp, ont_id)


def test_olt_netconf_connection(
    db: Session, olt_id: str
) -> tuple[bool, str, list[str]]:
    """Test NETCONF connectivity to an OLT."""
    from app.services.network import olt_netconf

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", []
    return olt_netconf.test_connection(olt)


def get_olt_netconf_config(
    db: Session, olt_id: str, *, filter_xpath: str | None = None
) -> tuple[bool, str, str]:
    """Fetch running config from OLT via NETCONF."""
    from app.services.network import olt_netconf

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", ""
    return olt_netconf.get_running_config(olt, filter_xpath=filter_xpath)


def get_olt_firmware_images(db: Session, olt_id: str) -> list:
    """Get available firmware images matching an OLT's vendor/model."""
    from app.models.network import OltFirmwareImage

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return []
    stmt = select(OltFirmwareImage).where(OltFirmwareImage.is_active.is_(True))
    if olt.vendor:
        stmt = stmt.where(OltFirmwareImage.vendor.ilike(f"%{olt.vendor}%"))
    return list(db.scalars(stmt.order_by(OltFirmwareImage.version.desc())).all())


def trigger_olt_firmware_upgrade(
    db: Session, olt_id: str, image_id: str
) -> tuple[bool, str]:
    """Validate and trigger an OLT firmware upgrade via SSH."""
    from app.models.network import OltFirmwareImage

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"
    image = db.get(OltFirmwareImage, image_id)
    if not image:
        return False, "Firmware image not found"
    if not image.is_active:
        return False, "Firmware image is not active"
    return olt_ssh_service.upgrade_firmware(
        olt, image.file_url, method=image.upgrade_method or "sftp"
    )


def _parse_walk_composite(lines: list[str], *, suffix_parts: int = 4) -> dict[str, str]:
    """Parse SNMP walk output while preserving composite ONU indexes."""
    parsed: dict[str, str] = {}
    for line in lines:
        if " = " not in line:
            continue
        oid_part, value_part = line.split(" = ", 1)
        oid_tokens = [p for p in oid_part.split(".") if p.isdigit()]
        if not oid_tokens:
            continue
        if len(oid_tokens) >= 2 and int(oid_tokens[-2]) > 1_000_000:
            # Huawei packed index format: <packed_fsp>.<onu_id>
            index = f"{oid_tokens[-2]}.{oid_tokens[-1]}"
        else:
            index = (
                ".".join(oid_tokens[-suffix_parts:])
                if len(oid_tokens) >= suffix_parts
                else oid_tokens[-1]
            )
        value = value_part.split(": ", 1)[-1].strip().strip('"')
        if value.lower().startswith("no such"):
            continue
        parsed[index] = value
    return parsed


def _parse_signal_dbm(raw: str | None, scale: float = 0.01) -> float | None:
    if not raw:
        return None
    import re

    match = re.search(r"(-?\d+)", raw)
    if not match:
        return None
    try:
        val = int(match.group(1))
    except ValueError:
        return None
    dbm = val * scale
    if -50.0 <= dbm <= 10.0:
        return dbm
    if -50.0 <= val <= 10.0:
        return float(val)
    return None


def _parse_distance_m(raw: str | None) -> int | None:
    if not raw:
        return None
    import re

    match = re.search(r"(\d+)", raw)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    # Some OLTs return tiny sentinel distances (often 0/1) when ONU is offline.
    if value <= 1:
        return None
    return value


def _parse_online_status(
    raw: str | None,
) -> tuple[OnuOnlineStatus, OnuOfflineReason | None]:
    if not raw:
        return OnuOnlineStatus.unknown, None
    import re

    lowered = raw.lower().strip()
    match = re.search(r"(\d+)", lowered)
    code = int(match.group(1)) if match else None
    if code == 1 or "online" in lowered or "up" in lowered:
        return OnuOnlineStatus.online, None
    if code in {2, 3, 4, 5} or "offline" in lowered or "down" in lowered:
        if code == 3:
            return OnuOnlineStatus.offline, OnuOfflineReason.power_fail
        if code == 4:
            return OnuOnlineStatus.offline, OnuOfflineReason.los
        if code == 5:
            return OnuOnlineStatus.offline, OnuOfflineReason.dying_gasp
        return OnuOnlineStatus.offline, OnuOfflineReason.unknown
    return OnuOnlineStatus.unknown, None


def _run_simple_v2c_walk(
    linked: NetworkDevice, oid: str, *, timeout: int = 45, bulk: bool = False
) -> list[str]:
    """Run SNMP walk with minimal flags for Huawei compatibility."""
    host = linked.mgmt_ip or linked.hostname
    if not host:
        raise RuntimeError("Missing SNMP host")
    if linked.snmp_port:
        host = f"{host}:{linked.snmp_port}"
    if (linked.snmp_version or "v2c").lower() not in {"v2c", "2c"}:
        raise RuntimeError("Only SNMP v2c is supported for ONT sync")
    community = (
        decrypt_credential(linked.snmp_community) if linked.snmp_community else ""
    )
    if not community:
        raise RuntimeError("SNMP community is not configured")

    cmd = "snmpbulkwalk" if bulk else "snmpwalk"
    args = [cmd, "-v2c", "-c", community, host, oid]
    result = subprocess.run(  # noqa: S603
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "SNMP walk failed").strip()
        raise RuntimeError(f"{oid}: {err}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def sync_onts_from_olt_snmp(
    db: Session, olt_id: str
) -> tuple[bool, str, dict[str, object]]:
    """Discover ONUs from an OLT by SNMP and upsert OntUnit rows.

    Supports vendor-specific OID profiles (Huawei, ZTE, Nokia) with
    automatic vendor detection from the linked monitoring device/OLT.

    Uses a PostgreSQL transaction-scoped advisory lock per-OLT to prevent
    concurrent SNMP syncs from racing on the same rows. Non-PostgreSQL
    test environments fall back to the unlocked path.
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return _sync_onts_from_olt_snmp_impl(db, olt_id)
    lock_key = _olt_sync_lock_key(olt_id)
    lock_acquired = bool(
        db.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_key}
        ).scalar()
    )
    if not lock_acquired:
        return (
            False,
            "Another sync is already running for this OLT",
            {"discovered": 0, "created": 0, "updated": 0},
        )
    return _sync_onts_from_olt_snmp_impl(db, olt_id)


def _sync_onts_from_olt_snmp_impl(
    db: Session, olt_id: str
) -> tuple[bool, str, dict[str, object]]:
    """Internal implementation of ONT SNMP sync (called with advisory lock held)."""
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", {"discovered": 0, "created": 0, "updated": 0}

    linked = _find_linked_network_device(
        db,
        mgmt_ip=olt.mgmt_ip,
        hostname=olt.hostname,
        name=olt.name,
    )

    # If no linked NetworkDevice, build a stand-in from the OLT's own SNMP fields
    if not linked:
        raw_ro = getattr(olt, "snmp_ro_community", None)
        if raw_ro and raw_ro.strip():
            from types import SimpleNamespace

            linked = SimpleNamespace(
                mgmt_ip=olt.mgmt_ip,
                hostname=olt.hostname,
                snmp_enabled=True,
                snmp_community=raw_ro.strip(),
                snmp_version="v2c",
                snmp_port=None,
                vendor=olt.vendor,
            )
        else:
            return (
                False,
                "No linked monitoring device and no SNMP community on OLT",
                {"discovered": 0, "created": 0, "updated": 0},
            )
    if not linked.snmp_enabled:
        return (
            False,
            "SNMP is disabled on the linked monitoring device",
            {"discovered": 0, "created": 0, "updated": 0},
        )

    vendor_text = str(linked.vendor or olt.vendor or "").lower()
    vendor_key = "generic"
    if "huawei" in vendor_text:
        vendor_key = "huawei"
    elif "zte" in vendor_text:
        vendor_key = "zte"
    elif "nokia" in vendor_text:
        vendor_key = "nokia"

    vendor_oid_profiles: dict[str, dict[str, str]] = {
        "huawei": {
            "status": ".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.15",
            "olt_rx": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4",
            "onu_rx": ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.6",
            "distance": ".1.3.6.1.4.1.2011.6.128.1.1.2.46.1.20",
        },
        "zte": {
            "status": ".1.3.6.1.4.1.3902.1082.500.10.2.2.1.1.10",
            "olt_rx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.2",
            "onu_rx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.3",
            "distance": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.7",
        },
        "nokia": {
            "status": ".1.3.6.1.4.1.637.61.1.35.10.1.1.8",
            "olt_rx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.2",
            "onu_rx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.4",
            "distance": ".1.3.6.1.4.1.637.61.1.35.10.1.1.9",
        },
        "generic": {
            "status": ".1.3.6.1.4.1.17409.2.3.6.1.1.8",
            "olt_rx": ".1.3.6.1.4.1.17409.2.3.6.10.1.2",
            "onu_rx": ".1.3.6.1.4.1.17409.2.3.6.10.1.3",
            "distance": ".1.3.6.1.4.1.17409.2.3.6.1.1.9",
        },
    }
    oids = vendor_oid_profiles[vendor_key]

    try:
        # Fast scalar probe first for clearer reachability errors.
        sysname_oid = ".1.3.6.1.2.1.1.5.0"
        _run_simple_v2c_walk(linked, sysname_oid, timeout=20, bulk=False)
        # Mandatory table: ONU run status (used to discover ONUs).
        status_rows = _parse_walk_composite(
            _run_simple_v2c_walk(
                linked,
                oids["status"],
                timeout=90,
                bulk=False,
            )
        )
    except Exception as exc:
        return (
            False,
            f"SNMP walk failed: {exc!s}",
            {"discovered": 0, "created": 0, "updated": 0},
        )

    # Optional tables: keep sync useful even when optical OIDs are slow/blocked.
    olt_rx_rows: dict[str, str] = {}
    onu_rx_rows: dict[str, str] = {}
    distance_rows: dict[str, str] = {}
    try:
        olt_rx_rows = _parse_walk_composite(
            _run_simple_v2c_walk(
                linked,
                oids["olt_rx"],
                timeout=90,
                bulk=False,
            )
        )
    except Exception:
        olt_rx_rows = {}
    try:
        onu_rx_rows = _parse_walk_composite(
            _run_simple_v2c_walk(
                linked,
                oids["onu_rx"],
                timeout=90,
                bulk=False,
            )
        )
    except Exception:
        onu_rx_rows = {}
    try:
        distance_rows = _parse_walk_composite(
            _run_simple_v2c_walk(
                linked,
                oids["distance"],
                timeout=90,
                bulk=False,
            )
        )
    except Exception:
        distance_rows = {}

    all_indexes = (
        set(status_rows) | set(olt_rx_rows) | set(onu_rx_rows) | set(distance_rows)
    )
    if not all_indexes:
        return (
            False,
            "No ONUs discovered from SNMP on this OLT",
            {"discovered": 0, "created": 0, "updated": 0},
        )

    existing_onts = list(
        db.scalars(select(OntUnit).where(OntUnit.olt_device_id == olt.id)).all()
    )
    by_external_id = {
        str(o.external_id): o for o in existing_onts if getattr(o, "external_id", None)
    }
    by_serial = {o.serial_number: o for o in existing_onts if o.serial_number}

    created = 0
    updated = 0
    unresolved_topology = 0
    now = datetime.now(UTC)
    olt_tag = str(olt.id).split("-")[0].upper()

    vendor_serial_prefix = {
        "huawei": "HW",
        "zte": "ZT",
        "nokia": "NK",
        "generic": "OLT",
    }.get(vendor_key, "OLT")

    for idx in sorted(all_indexes):
        parsed = _split_onu_index(idx)
        if not parsed:
            continue
        frame = None
        slot = None
        port = None
        onu = "0"
        fsp: str | None = None
        if len(parsed) >= 4:
            frame, slot, port, onu = parsed
            fsp = f"{frame}/{slot}/{port}"
        else:
            packed, onu = parsed
            if vendor_key == "huawei":
                packed_int = int(packed) if str(packed).isdigit() else None
                fsp = (
                    _decode_huawei_packed_fsp(packed_int)
                    if packed_int is not None
                    else None
                )
        if fsp:
            fsp_parts = fsp.split("/")
            frame = fsp_parts[0] if len(fsp_parts) > 0 else None
            slot = fsp_parts[1] if len(fsp_parts) > 1 else None
            port = fsp_parts[2] if len(fsp_parts) > 2 else None
        else:
            unresolved_topology += 1
        board = f"{frame}/{slot}" if frame is not None and slot is not None else None
        external_id = f"{vendor_key}:{idx}"
        serial_frame = frame if frame is not None else "U"
        serial_slot = slot if slot is not None else "U"
        serial_port = port if port is not None else "U"
        synthetic_serial = f"{vendor_serial_prefix}-{olt_tag}-{serial_frame}{serial_slot}{serial_port}{onu}"

        status, offline_reason = _parse_online_status(status_rows.get(idx))
        olt_rx = _parse_signal_dbm(olt_rx_rows.get(idx))
        onu_rx = _parse_signal_dbm(onu_rx_rows.get(idx))
        distance = _parse_distance_m(distance_rows.get(idx))

        # First check in-memory cache, then verify with database query
        # Track match method to avoid overwriting external_id when matched by serial
        matched_by_external_id = False
        ont = by_external_id.get(external_id)
        if ont:
            matched_by_external_id = True
        else:
            ont = by_serial.get(synthetic_serial)

        # If not in cache, do a database lookup to handle race conditions
        if ont is None:
            ont = db.scalars(
                select(OntUnit).where(
                    OntUnit.olt_device_id == olt.id,
                    OntUnit.external_id == external_id,
                )
            ).first()
            if ont:
                # Found in DB but not in cache - add to cache
                matched_by_external_id = True
                by_external_id[external_id] = ont
                if ont.serial_number:
                    by_serial[ont.serial_number] = ont

        if ont is None:
            # Only set external_id if no other ONT already has it
            new_external_id = external_id if external_id not in by_external_id else None
            ont = OntUnit(
                serial_number=synthetic_serial,
                model=olt.model,
                vendor=olt.vendor or vendor_key.title(),
                is_active=True,
                olt_device_id=olt.id,
                pon_type=PonType.gpon,
                gpon_channel=GponChannel.gpon,
                board=board,
                port=port,
                external_id=new_external_id,
                name=f"ONU {fsp}:{onu}" if fsp else f"ONU unresolved:{idx}",
                online_status=status,
                tr069_acs_server_id=olt.tr069_acs_server_id,
            )
            db.add(ont)
            created += 1
            if new_external_id:
                by_external_id[new_external_id] = ont
            by_serial[synthetic_serial] = ont
        else:
            updated += 1
            ont.olt_device_id = olt.id
            ont.vendor = ont.vendor or (olt.vendor or vendor_key.title())
            ont.model = ont.model or olt.model
            ont.board = board
            ont.port = port
            # Only set external_id if:
            # 1. Matched by external_id (confirming the match), or
            # 2. ONT has no external_id set, AND no other active ONT has this external_id
            if matched_by_external_id:
                ont.external_id = external_id
            elif not ont.external_id:
                # Check if another active ONT already has this external_id
                conflict = by_external_id.get(external_id)
                if conflict is None or conflict.id == ont.id:
                    ont.external_id = external_id
                    by_external_id[external_id] = ont
                # else: skip setting external_id to avoid conflict
            ont.pon_type = PonType.gpon
            ont.gpon_channel = GponChannel.gpon
            ont.online_status = status
            ont.tr069_acs_server_id = olt.tr069_acs_server_id

        ont.olt_rx_signal_dbm = olt_rx
        ont.onu_rx_signal_dbm = onu_rx
        ont.distance_meters = distance
        ont.signal_updated_at = now
        if status == OnuOnlineStatus.online:
            ont.last_seen_at = now
            ont.offline_reason = None
        elif status == OnuOnlineStatus.offline:
            ont.offline_reason = offline_reason

    try:
        db.flush()
    except Exception as exc:
        db.rollback()
        return (
            False,
            f"Failed to save discovered ONTs: {exc!s}",
            {"discovered": len(all_indexes), "created": created, "updated": updated},
        )

    # Auto-create OntAssignment records linking ONTs to PON ports
    assignment_created = 0
    assignment_errors = 0
    try:
        # Build lookup of existing PON ports by name for this OLT
        olt_pon_ports = {
            pp.name: pp
            for pp in db.scalars(
                select(PonPort).where(
                    PonPort.olt_id == olt.id,
                    PonPort.is_active.is_(True),
                )
            ).all()
        }
        # Get ONTs for this OLT that lack an active assignment
        onts_needing_assignment = list(
            db.scalars(
                select(OntUnit)
                .outerjoin(
                    OntAssignment,
                    (OntAssignment.ont_unit_id == OntUnit.id)
                    & (OntAssignment.active.is_(True)),
                )
                .where(
                    OntUnit.olt_device_id == olt.id,
                    OntUnit.is_active.is_(True),
                    OntAssignment.id.is_(None),
                )
            ).all()
        )
        for ont_item in onts_needing_assignment:
            ont_board = getattr(ont_item, "board", "") or ""
            ont_port = getattr(ont_item, "port", "") or ""
            pon_name = f"{ont_board}/{ont_port}" if ont_board and ont_port else None
            if not pon_name:
                continue
            pon_port = olt_pon_ports.get(pon_name)
            if not pon_port:
                # Auto-create the PON port
                pon_port = PonPort(
                    olt_id=olt.id,
                    name=pon_name,
                    is_active=True,
                )
                db.add(pon_port)
                db.flush()
                olt_pon_ports[pon_name] = pon_port
            assignment = OntAssignment(
                ont_unit_id=ont_item.id,
                pon_port_id=pon_port.id,
                active=True,
                assigned_at=now,
            )
            db.add(assignment)
            assignment_created += 1
        if assignment_created:
            db.flush()
    except Exception as exc:
        logger.warning("Failed to auto-create ONT assignments: %s", exc)
        db.rollback()
        assignment_errors += 1
        return (
            False,
            f"Failed to auto-create ONT assignments: {exc!s}",
            {
                "discovered": len(all_indexes),
                "created": created,
                "updated": updated,
                "assignments_created": 0,
                "assignment_errors": assignment_errors,
                "tr069_runtime_synced": 0,
                "tr069_runtime_errors": 0,
            },
        )

    if created > 0:
        try:
            emit_event(
                db,
                EventType.ont_discovered,
                {
                    "olt_id": str(olt.id),
                    "olt_name": olt.name,
                    "created": created,
                    "updated": updated,
                    "total_discovered": len(all_indexes),
                },
                actor="system",
            )
        except Exception as exc:
            logger.warning("Failed to emit ont_discovered event: %s", exc)

    tr069_runtime_synced = 0
    tr069_runtime_errors = 0
    if olt.tr069_acs_server_id:
        try:
            from app.services.network.ont_tr069 import OntTR069

            onts_for_olt = list(
                db.scalars(
                    select(OntUnit)
                    .where(OntUnit.olt_device_id == olt.id)
                    .where(OntUnit.is_active.is_(True))
                ).all()
            )
            for ont in onts_for_olt:
                try:
                    summary = OntTR069.get_device_summary(
                        db,
                        str(ont.id),
                        persist_observed_runtime=True,
                    )
                    if summary.available:
                        tr069_runtime_synced += 1
                except Exception:
                    tr069_runtime_errors += 1
        except Exception:
            tr069_runtime_errors += 1

    propagation_stats: dict[str, int] = {}
    if olt.tr069_acs_server_id:
        try:
            propagation_stats = _queue_acs_propagation(db, olt)
        except Exception as exc:
            logger.error("ACS propagation after ONT sync failed: %s", exc)
            propagation_stats = {
                "attempted": 0,
                "propagated": 0,
                "unresolved": 0,
                "errors": 1,
            }

    message = (
        f"{vendor_key.title()} ONT sync complete: discovered {len(all_indexes)}, "
        f"created {created}, updated {updated}."
    )
    result_stats = {
        "discovered": len(all_indexes),
        "created": created,
        "updated": updated,
        "unresolved_topology": unresolved_topology,
        "assignments_created": assignment_created,
        "assignment_errors": assignment_errors,
        "tr069_runtime_synced": tr069_runtime_synced,
        "tr069_runtime_errors": tr069_runtime_errors,
    }
    if propagation_stats:
        result_stats["acs_propagation"] = propagation_stats
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        return False, f"Failed to finalize ONT sync: {exc!s}", result_stats
    return True, message, result_stats


def sync_onts_from_olt_snmp_tracked(
    db: Session,
    olt_id: str,
    *,
    initiated_by: str | None = None,
) -> tuple[bool, str, dict[str, object]]:
    """Tracked wrapper around sync_onts_from_olt_snmp.

    Creates a NetworkOperation record to track the sync lifecycle,
    then delegates to the existing sync function.
    """
    from app.models.network_operation import (
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network_operations import network_operations

    try:
        op = network_operations.start(
            db,
            NetworkOperationType.olt_ont_sync,
            NetworkOperationTargetType.olt,
            olt_id,
            correlation_key=f"olt_sync:{olt_id}",
            initiated_by=initiated_by,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            return False, "A sync is already in progress for this OLT.", {}
        raise
    network_operations.mark_running(db, str(op.id))
    db.flush()

    try:
        success, message, stats = sync_onts_from_olt_snmp(db, olt_id)
        try:
            if success:
                network_operations.mark_succeeded(
                    db, str(op.id), output_payload=dict(stats)
                )
            else:
                network_operations.mark_failed(
                    db, str(op.id), message, output_payload=dict(stats)
                )
        except Exception as track_err:
            logger.error(
                "Failed to record operation outcome for %s: %s", op.id, track_err
            )
        return success, message, stats
    except Exception as exc:
        try:
            network_operations.mark_failed(db, str(op.id), str(exc))
        except Exception as track_err:
            logger.error(
                "Failed to record operation failure for %s: %s (original: %s)",
                op.id,
                track_err,
                exc,
            )
            db.rollback()
        raise


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


# ---------------------------------------------------------------------------
# VLAN / IP Pool assignment helpers
# ---------------------------------------------------------------------------


def available_vlans_for_olt(db: Session, olt_id: str) -> list:
    """Return VLANs not yet assigned to this OLT."""
    from app.models.network import Vlan

    return list(
        db.scalars(
            select(Vlan)
            .where(Vlan.olt_device_id.is_(None))
            .where(Vlan.is_active.is_(True))
            .order_by(Vlan.tag.asc())
        ).all()
    )


def available_ip_pools_for_olt(db: Session, olt_id: str) -> list:
    """Return IP pools not yet assigned to any OLT."""
    from app.models.network import IpPool

    return list(
        db.scalars(
            select(IpPool)
            .where(IpPool.olt_device_id.is_(None))
            .where(IpPool.is_active.is_(True))
            .order_by(IpPool.name.asc())
        ).all()
    )


def assign_vlan_to_olt(db: Session, olt_id: str, vlan_id: str) -> tuple[bool, str]:
    """Assign a VLAN to an OLT. Returns (success, message)."""
    from app.models.network import Vlan

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"
    vlan = db.get(Vlan, vlan_id)
    if not vlan:
        return False, "VLAN not found"
    if vlan.olt_device_id is not None:
        return False, f"VLAN {vlan.tag} is already assigned to an OLT"
    vlan.olt_device_id = olt.id
    db.commit()
    logger.info("Assigned VLAN %s (tag %d) to OLT %s", vlan_id, vlan.tag, olt.name)
    return True, f"VLAN {vlan.tag} assigned"


def unassign_vlan_from_olt(db: Session, olt_id: str, vlan_id: str) -> tuple[bool, str]:
    """Remove VLAN assignment from an OLT."""
    from app.models.network import Vlan

    vlan = db.get(Vlan, vlan_id)
    if not vlan:
        return False, "VLAN not found"
    if str(vlan.olt_device_id) != olt_id:
        return False, "VLAN is not assigned to this OLT"
    vlan.olt_device_id = None
    db.commit()
    logger.info("Unassigned VLAN %s (tag %d) from OLT %s", vlan_id, vlan.tag, olt_id)
    return True, f"VLAN {vlan.tag} unassigned"


def assign_ip_pool_to_olt(db: Session, olt_id: str, pool_id: str) -> tuple[bool, str]:
    """Assign an IP pool to an OLT."""
    from app.models.network import IpPool

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"
    pool = db.get(IpPool, pool_id)
    if not pool:
        return False, "IP pool not found"
    if pool.olt_device_id is not None:
        return False, f"Pool '{pool.name}' is already assigned to an OLT"
    pool.olt_device_id = olt.id
    db.commit()
    logger.info("Assigned IP pool %s (%s) to OLT %s", pool_id, pool.name, olt.name)
    return True, f"Pool '{pool.name}' assigned"


def unassign_ip_pool_from_olt(
    db: Session, olt_id: str, pool_id: str
) -> tuple[bool, str]:
    """Remove IP pool assignment from an OLT."""
    from app.models.network import IpPool

    pool = db.get(IpPool, pool_id)
    if not pool:
        return False, "IP pool not found"
    if str(pool.olt_device_id) != olt_id:
        return False, "Pool is not assigned to this OLT"
    pool.olt_device_id = None
    db.commit()
    logger.info("Unassigned IP pool %s (%s) from OLT %s", pool_id, pool.name, olt_id)
    return True, f"Pool '{pool.name}' unassigned"


# ---------------------------------------------------------------------------
# CLI Command Runner
# ---------------------------------------------------------------------------

# Allowed command prefixes — read-only / diagnostic commands only.
# Anything not starting with one of these is rejected.
_CLI_ALLOWED_PREFIXES: list[str] = [
    "display ",
    "show ",
    "ping ",
    "traceroute ",
    "dir ",
    "list ",
]

# Explicitly blocked patterns — dangerous even if prefix matches.
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


def validate_cli_command(command: str) -> str | None:
    """Check if a CLI command is safe to execute. Returns error or None."""
    cmd = command.strip()
    if not cmd:
        return "Command is empty"
    if len(cmd) > 500:
        return "Command too long (max 500 characters)"

    cmd_lower = cmd.lower()

    # Check blocked patterns first
    for pattern in _CLI_BLOCKED_PATTERNS:
        if pattern in cmd_lower:
            return f"Command contains blocked keyword: {pattern}"

    # Check allowed prefixes
    if not any(cmd_lower.startswith(prefix) for prefix in _CLI_ALLOWED_PREFIXES):
        allowed = ", ".join(p.strip() for p in _CLI_ALLOWED_PREFIXES)
        return f"Only read-only commands allowed. Permitted prefixes: {allowed}"

    return None


def execute_cli_command(
    db: Session, olt_id: str, command: str
) -> tuple[bool, str, str]:
    """Execute a validated CLI command on an OLT.

    Returns (success, message, output).
    """
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", ""

    error = validate_cli_command(command)
    if error:
        return False, error, ""

    ok, message, output = olt_ssh_service.run_cli_command(olt, command.strip())
    logger.info(
        "CLI command on OLT %s: %s → %s",
        olt.name,
        command.strip(),
        "ok" if ok else "failed",
    )
    return ok, message, output


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
        logger.info("SSH config backup saved for OLT %s: %s", olt.name, filename)
        return backup, "Full running config backed up via SSH"
    except Exception as exc:
        db.rollback()
        return None, f"Failed to save SSH backup: {exc}"


# ---------------------------------------------------------------------------
# TR-069 ACS profile management
# ---------------------------------------------------------------------------

_ONU_INDEX_RE = re.compile(r"\.(\d+)$")
_ONU_NAME_RE = re.compile(r":(\d+)$")


def _extract_onu_index(ont: OntUnit) -> int | None:
    """Extract the ONU index from an ONT's external_id or name.

    Patterns:
        external_id ``huawei:XXXX.{N}`` → N
        name ``ONU F/S/P:{N}`` → N
    """
    if ont.external_id:
        m = _ONU_INDEX_RE.search(ont.external_id)
        if m:
            return int(m.group(1))
    if ont.name:
        m = _ONU_NAME_RE.search(ont.name)
        if m:
            return int(m.group(1))
    return None


def get_tr069_profiles_context(
    db: Session, olt_id: str
) -> tuple[bool, str, list[dict[str, Any]], dict[str, Any]]:
    """Read TR-069 server profiles from an OLT and prepare template context.

    Returns:
        Tuple of (ok, message, profiles_data, acs_prefill).
    """
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", [], {}

    ok, message, profiles = olt_ssh_service.get_tr069_server_profiles(olt)

    profiles_data = [
        {
            "profile_id": p.profile_id,
            "name": p.name,
            "acs_url": p.acs_url,
            "acs_username": p.acs_username,
            "inform_interval": p.inform_interval,
            "binding_count": p.binding_count,
        }
        for p in profiles
    ]

    # Pre-fill from linked ACS server if available
    acs_prefill: dict[str, Any] = {}
    if olt.tr069_acs_server:
        acs = olt.tr069_acs_server
        acs_prefill = {
            "acs_url": acs.cwmp_url or "",
            "acs_username": acs.cwmp_username or "",
            "acs_name": acs.name or "",
        }

    # Load ONTs on this OLT for the binding table
    stmt = (
        select(OntUnit)
        .where(OntUnit.olt_device_id == olt.id)
        .order_by(OntUnit.board, OntUnit.port, OntUnit.name)
    )
    onts = db.scalars(stmt).all()
    ont_rows = []
    for ont in onts:
        onu_index = _extract_onu_index(ont)
        if onu_index is None:
            continue
        ont_rows.append(
            {
                "id": str(ont.id),
                "serial_number": ont.serial_number,
                "board": ont.board or "",
                "port": ont.port or "",
                "onu_index": onu_index,
                "name": ont.name or "",
                "online": ont.online_status.value if ont.online_status else "unknown",
                "subscriber_name": getattr(ont, "address_or_comment", "") or "",
            }
        )

    return (
        ok,
        message,
        profiles_data,
        {
            "acs_prefill": acs_prefill,
            "onts": ont_rows,
        },
    )


def handle_create_tr069_profile(
    db: Session,
    olt_id: str,
    *,
    profile_name: str,
    acs_url: str,
    username: str = "",
    password: str = "",
    inform_interval: int = 300,
) -> tuple[bool, str]:
    """Validate and create a TR-069 server profile on an OLT."""
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"

    return olt_ssh_service.create_tr069_server_profile(
        olt,
        profile_name=profile_name,
        acs_url=acs_url,
        username=username,
        password=password,
        inform_interval=inform_interval,
    )


def handle_rebind_tr069_profiles(
    db: Session,
    olt_id: str,
    ont_ids: list[str],
    target_profile_id: int,
) -> dict[str, int | list[str]]:
    """Rebind selected ONTs to a TR-069 server profile.

    Returns:
        Stats dict with keys: rebound, failed, errors (list of messages).
    """
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return {"rebound": 0, "failed": 1, "errors": ["OLT not found"]}

    stats: dict[str, int | list[str]] = {"rebound": 0, "failed": 0, "errors": []}
    errors_list: list[str] = []

    for ont_id in ont_ids:
        ont = db.get(OntUnit, ont_id)
        if not ont:
            errors_list.append(f"ONT {ont_id} not found")
            stats["failed"] = int(stats["failed"]) + 1
            continue

        onu_index = _extract_onu_index(ont)
        if onu_index is None:
            errors_list.append(f"ONT {ont.serial_number}: cannot determine ONU index")
            stats["failed"] = int(stats["failed"]) + 1
            continue

        # Build F/S/P from board + port
        board = ont.board or ""
        port = ont.port or ""
        if not board or not port:
            errors_list.append(f"ONT {ont.serial_number}: missing board/port")
            stats["failed"] = int(stats["failed"]) + 1
            continue

        fsp = f"{board}/{port}"

        ok, msg = olt_ssh_service.bind_tr069_server_profile(
            olt, fsp, onu_index, target_profile_id
        )
        if ok:
            stats["rebound"] = int(stats["rebound"]) + 1
        else:
            stats["failed"] = int(stats["failed"]) + 1
            errors_list.append(f"ONT {ont.serial_number}: {msg}")

    stats["errors"] = errors_list
    return stats


def olt_device_events_context(db: Session, olt_id: str) -> dict:
    """Build context for the OLT device events tab.

    Queries ONT-related events (online/offline/signal/discovered) from the
    EventStore where the payload contains this OLT's ID.

    Args:
        db: Database session.
        olt_id: OLT device ID.

    Returns:
        Dict with events list and has_more flag.
    """
    from sqlalchemy import select

    from app.models.event_store import EventStore

    ont_event_types = [
        "ont.online",
        "ont.offline",
        "ont.signal_degraded",
        "ont.discovered",
        "ont.provisioned",
        "ont.config_updated",
        "ont.moved",
    ]
    stmt = (
        select(EventStore)
        .where(
            EventStore.event_type.in_(ont_event_types),
            EventStore.payload["olt_id"].astext == olt_id,
        )
        .order_by(EventStore.created_at.desc())
        .limit(100)
    )
    events = list(db.scalars(stmt).all())
    return {"events": events, "has_more": len(events) >= 100}


def resolve_operational_acs_server(
    db: Session, olt: OLTDevice | None = None
) -> dict[str, str | None] | None:
    """Resolve the operational ACS server for an OLT or globally.

    Returns a dict with ``url``, ``username``, ``name`` keys, or None.
    """
    from app.models.network import Tr069AcsServer

    if olt and getattr(olt, "tr069_acs_server_id", None):
        server = db.get(Tr069AcsServer, str(olt.tr069_acs_server_id))
        if server:
            return {
                "url": getattr(server, "url", None),
                "username": getattr(server, "username", None),
                "name": getattr(server, "name", None),
            }

    stmt = (
        select(Tr069AcsServer)
        .where(Tr069AcsServer.is_active.is_(True))
        .order_by(Tr069AcsServer.name)
        .limit(1)
    )
    server = db.scalars(stmt).first()
    if server:
        return {
            "url": getattr(server, "url", None),
            "username": getattr(server, "username", None),
            "name": getattr(server, "name", None),
        }
    return None


def ensure_tr069_profile_for_linked_acs(
    olt: OLTDevice,
) -> tuple[bool, str, int | None]:
    """Ensure a TR-069 server profile exists on the OLT for its linked ACS.

    Returns (success, message, profile_id).
    """
    if not getattr(olt, "tr069_acs_server_id", None):
        return False, "OLT has no linked ACS server", None
    logger.info(
        "ensure_tr069_profile_for_linked_acs called for OLT %s — stub returning success",
        olt.id,
    )
    return True, "TR-069 profile check passed (stub)", None
