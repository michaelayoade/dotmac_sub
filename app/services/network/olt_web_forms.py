"""OLT form parsing, validation, persistence, and audit helpers."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.network import OLTDevice, OntProvisioningProfile, OntUnit
from app.models.network_monitoring import DeviceRole, DeviceType, NetworkDevice
from app.models.tr069 import Tr069AcsServer
from app.schemas.network import OLTDeviceCreate, OLTDeviceUpdate
from app.services import network as network_service
from app.services import tr069 as tr069_service
from app.services.audit_helpers import diff_dicts, model_to_dict
from app.services.common import coerce_uuid
from app.services.credential_crypto import decrypt_credential, encrypt_credential
from app.services.network.olt_inventory import get_olt_or_none as get_olt_or_none
from app.services.network.olt_monitoring_devices import find_linked_network_device
from app.services.network.olt_web_audit import (
    actor_id_from_request,
    log_olt_audit_event,
)

logger = logging.getLogger(__name__)


def _encrypt_if_set(values: Mapping[str, Any], key: str) -> str | None:
    """Extract a string value from form data, encrypt if non-empty."""
    raw = str(values.get(key) or "").strip() or None
    if raw:
        return encrypt_credential(raw)
    return None


def integrity_error_message(exc: Exception) -> str:
    """Map OLT integrity errors to user-facing strings."""
    message = str(exc)
    if "uq_olt_devices_hostname" in message:
        return "Hostname already exists"
    if "uq_olt_devices_mgmt_ip" in message:
        return "Management IP already exists"
    return "OLT device could not be saved due to a data conflict"


def _parse_int_or_none(raw: str) -> int | None:
    """Parse string to int, returning None for empty or non-numeric values."""
    raw = raw.strip() if raw else ""
    return int(raw) if raw.isdigit() else None


def _parse_uuid_or_none(raw: str) -> str | None:
    """Parse string to UUID string, returning None for empty values."""
    raw = raw.strip() if raw else ""
    return raw if raw else None


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
        "default_provisioning_profile_id": (
            form.get("default_provisioning_profile_id", "").strip() or None
        ),
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
        # -------------------------------------------------------------------------
        # Config Pack fields (ONT Provisioning Defaults)
        # -------------------------------------------------------------------------
        # Authorization profiles
        "default_line_profile_id": _parse_int_or_none(
            str(form.get("default_line_profile_id", ""))
        ),
        "default_service_profile_id": _parse_int_or_none(
            str(form.get("default_service_profile_id", ""))
        ),
        # VLANs by purpose
        "internet_vlan_id": _parse_uuid_or_none(str(form.get("internet_vlan_id", ""))),
        "management_vlan_id": _parse_uuid_or_none(
            str(form.get("management_vlan_id", ""))
        ),
        "tr069_vlan_id": _parse_uuid_or_none(str(form.get("tr069_vlan_id", ""))),
        "voip_vlan_id": _parse_uuid_or_none(str(form.get("voip_vlan_id", ""))),
        "iptv_vlan_id": _parse_uuid_or_none(str(form.get("iptv_vlan_id", ""))),
        # GEM indices
        "default_internet_gem_index": _parse_int_or_none(
            str(form.get("default_internet_gem_index", ""))
        ),
        "default_mgmt_gem_index": _parse_int_or_none(
            str(form.get("default_mgmt_gem_index", ""))
        ),
        "default_voip_gem_index": _parse_int_or_none(
            str(form.get("default_voip_gem_index", ""))
        ),
        "default_iptv_gem_index": _parse_int_or_none(
            str(form.get("default_iptv_gem_index", ""))
        ),
        # Provisioning knobs
        "default_tr069_olt_profile_id": _parse_int_or_none(
            str(form.get("default_tr069_olt_profile_id", ""))
        ),
        "default_internet_config_ip_index": _parse_int_or_none(
            str(form.get("default_internet_config_ip_index", ""))
        ),
        "default_wan_config_profile_id": _parse_int_or_none(
            str(form.get("default_wan_config_profile_id", ""))
        ),
        # Management IP pool
        "mgmt_ip_pool_id": _parse_uuid_or_none(str(form.get("mgmt_ip_pool_id", ""))),
        # Connection request credentials
        "default_cr_username": form.get("default_cr_username", "").strip() or None,
        "default_cr_password": form.get("default_cr_password", "").strip() or None,
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

    # Validate default_provisioning_profile_id if provided
    default_profile_id = values.get("default_provisioning_profile_id")
    if default_profile_id:
        profile = db.get(OntProvisioningProfile, coerce_uuid(default_profile_id))
        if not profile:
            return "Default provisioning profile not found."
        if not profile.is_active:
            return f"Default provisioning profile '{profile.name}' is inactive."
        # If OLT already exists, check profile is scoped to this OLT or is global
        if current_olt and profile.olt_device_id:
            if profile.olt_device_id != current_olt.id:
                return f"Bundle '{profile.name}' is scoped to a different OLT."

    # Validate tr069_acs_server_id if provided
    acs_server_id = values.get("tr069_acs_server_id")
    if acs_server_id:
        acs_server = db.get(Tr069AcsServer, coerce_uuid(acs_server_id))
        if not acs_server:
            return "TR-069 ACS server not found."
        if not acs_server.is_active:
            return f"TR-069 ACS server '{acs_server.name}' is inactive."

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
            "default_provisioning_profile_id": values.get(
                "default_provisioning_profile_id"
            ),
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
    # Encrypt CR password if provided
    cr_password = values.get("default_cr_password")
    encrypted_cr_password = encrypt_credential(
        cr_password if isinstance(cr_password, str) else None
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
        "default_provisioning_profile_id": values.get(
            "default_provisioning_profile_id"
        ),
        "notes": values.get("notes"),
        "is_active": values.get("is_active"),
        # -------------------------------------------------------------------------
        # Config Pack fields (ONT Provisioning Defaults)
        # -------------------------------------------------------------------------
        # Authorization profiles
        "default_line_profile_id": values.get("default_line_profile_id"),
        "default_service_profile_id": values.get("default_service_profile_id"),
        # VLANs by purpose
        "internet_vlan_id": values.get("internet_vlan_id"),
        "management_vlan_id": values.get("management_vlan_id"),
        "tr069_vlan_id": values.get("tr069_vlan_id"),
        "voip_vlan_id": values.get("voip_vlan_id"),
        "iptv_vlan_id": values.get("iptv_vlan_id"),
        # GEM indices
        "default_internet_gem_index": values.get("default_internet_gem_index"),
        "default_mgmt_gem_index": values.get("default_mgmt_gem_index"),
        "default_voip_gem_index": values.get("default_voip_gem_index"),
        "default_iptv_gem_index": values.get("default_iptv_gem_index"),
        # Provisioning knobs
        "default_tr069_olt_profile_id": values.get("default_tr069_olt_profile_id"),
        "default_internet_config_ip_index": values.get(
            "default_internet_config_ip_index"
        ),
        "default_wan_config_profile_id": values.get("default_wan_config_profile_id"),
        # Management IP pool
        "mgmt_ip_pool_id": values.get("mgmt_ip_pool_id"),
        # Connection request credentials
        "default_cr_username": values.get("default_cr_username"),
        "default_cr_password": encrypted_cr_password,
    }
    if "supported_pon_types" in values:
        data["supported_pon_types"] = values["supported_pon_types"]
    if "status" in values and values["status"] is not None:
        data["status"] = values["status"]
    return OLTDeviceUpdate.model_validate(data)


_find_linked_network_device = find_linked_network_device


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
    snmp_port = values.get("snmp_port")
    linked.snmp_port = snmp_port if isinstance(snmp_port, int) else 161
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
        default_provisioning_profile_id=olt.default_provisioning_profile_id,
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
        # Config Pack fields
        default_line_profile_id=getattr(olt, "default_line_profile_id", None),
        default_service_profile_id=getattr(olt, "default_service_profile_id", None),
        internet_vlan_id=getattr(olt, "internet_vlan_id", None),
        management_vlan_id=getattr(olt, "management_vlan_id", None),
        tr069_vlan_id=getattr(olt, "tr069_vlan_id", None),
        voip_vlan_id=getattr(olt, "voip_vlan_id", None),
        iptv_vlan_id=getattr(olt, "iptv_vlan_id", None),
        default_internet_gem_index=getattr(olt, "default_internet_gem_index", None),
        default_mgmt_gem_index=getattr(olt, "default_mgmt_gem_index", None),
        default_voip_gem_index=getattr(olt, "default_voip_gem_index", None),
        default_iptv_gem_index=getattr(olt, "default_iptv_gem_index", None),
        default_tr069_olt_profile_id=getattr(olt, "default_tr069_olt_profile_id", None),
        default_internet_config_ip_index=getattr(
            olt, "default_internet_config_ip_index", None
        ),
        default_wan_config_profile_id=getattr(
            olt, "default_wan_config_profile_id", None
        ),
        mgmt_ip_pool_id=getattr(olt, "mgmt_ip_pool_id", None),
        default_cr_username=getattr(olt, "default_cr_username", None),
        default_cr_password=getattr(olt, "default_cr_password", None),
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

    inform_interval = str(
        server.periodic_inform_interval or settings.tr069_periodic_inform_interval
    )
    acs_params: dict[str, str] = {
        "Device.ManagementServer.URL": server.cwmp_url,
        "Device.ManagementServer.PeriodicInformEnable": "true",
        "Device.ManagementServer.PeriodicInformInterval": inform_interval,
        "InternetGatewayDevice.ManagementServer.URL": server.cwmp_url,
        "InternetGatewayDevice.ManagementServer.PeriodicInformEnable": "true",
        "InternetGatewayDevice.ManagementServer.PeriodicInformInterval": (
            inform_interval
        ),
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

    # Send both TR-098 (InternetGatewayDevice) and TR-181 (Device) parameters.
    # Devices will ignore unsupported paths, so sending both is safe and avoids
    # needing to detect the data model before propagation.

    for ont in onts:
        stats["attempted"] += 1
        try:
            resolved, reason = resolve_genieacs_with_reason(db, ont)
            if resolved:
                client, device_id = resolved
                # Fire and forget: send params without strict verification since
                # only one data model will apply (device ignores unsupported paths).
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
        # Preserve CR password when form doesn't submit new value
        if payload_values.get("default_cr_password") is None:
            payload_values["default_cr_password"] = getattr(
                current, "default_cr_password", None
            )
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
            onts = (
                db.query(OntUnit)
                .filter(OntUnit.olt_device_id == olt.id)
                .filter(OntUnit.is_active.is_(True))
                .all()
            )
            for ont in onts:
                tr069_service.sync_ont_acs_server(db, ont, olt.tr069_acs_server_id)
            db.commit()
            _queue_acs_propagation(db, olt)
        return olt, None
    except IntegrityError as exc:
        logger.warning("OLT update integrity error for %s: %s", olt_id, exc)
        db.rollback()
        return None, integrity_error_message(exc)


def _auto_init_tr069_profile(olt: OLTDevice) -> None:
    """Best-effort: create the linked ACS TR-069 profile on a new OLT.

    Runs after OLT creation. Silently skips if SSH is not configured
    or if profile creation fails (admin can use the Init TR-069 button later).
    """
    if not olt.ssh_username or not olt.ssh_password:
        logger.info("Skipping auto TR-069 init for %s — no SSH credentials", olt.name)
        return
    try:
        from app.services.network.olt_tr069_admin import (
            ensure_tr069_profile_for_linked_acs,
        )

        ok, msg, profile_id = ensure_tr069_profile_for_linked_acs(olt)
        if not ok:
            logger.info("Skipping auto TR-069 init for %s: %s", olt.name, msg)
        elif profile_id is not None:
            logger.info("Auto-verified TR-069 profile %s on %s", profile_id, olt.name)
        else:
            logger.info("Auto-verified TR-069 profile on %s", olt.name)
    except Exception as exc:
        logger.warning("Auto TR-069 init error on %s: %s", olt.name, exc)


def create_olt_with_audit(
    db: Session,
    request: Request,
    values: dict[str, object],
    actor_id: str | None = None,
) -> tuple[OLTDevice | None, str | None]:
    """Create OLT, log audit event, and return result."""
    olt, error = create_olt(db, values)
    if error or olt is None:
        return olt, error
    log_olt_audit_event(
        db,
        request=request,
        action="create",
        entity_id=str(olt.id),
        metadata={"name": olt.name, "mgmt_ip": olt.mgmt_ip or None},
    )
    if actor_id and actor_id != actor_id_from_request(request):
        logger.debug("Ignoring explicit OLT audit actor_id; request actor is canonical")

    # Auto-create the linked ACS TR-069 profile on the new OLT (best-effort).
    _auto_init_tr069_profile(olt)

    return olt, None


def update_olt_with_audit(
    db: Session,
    request: Request,
    olt_id: str,
    before_obj: OLTDevice,
    values: dict[str, object],
    actor_id: str | None = None,
) -> tuple[OLTDevice | None, str | None]:
    """Update OLT, compute diff, log audit event, and return result."""
    before_snapshot = model_to_dict(before_obj)
    olt, error = update_olt(db, olt_id, values)
    if error or olt is None:
        return olt, error
    after_obj = network_service.olt_devices.get(db=db, device_id=olt_id)
    after_snapshot = model_to_dict(after_obj)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata_payload: dict[str, object] | None = (
        {"changes": changes} if changes else None
    )
    log_olt_audit_event(
        db,
        request=request,
        action="update",
        entity_id=str(olt_id),
        metadata=metadata_payload,
    )
    if actor_id and actor_id != actor_id_from_request(request):
        logger.debug("Ignoring explicit OLT audit actor_id; request actor is canonical")
    return olt, None


def snapshot(values: dict[str, object]) -> SimpleNamespace:
    """Build simple object for form re-render on errors."""
    return SimpleNamespace(**values)
