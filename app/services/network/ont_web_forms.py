"""Service helpers for admin ONT form flows."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.requests import Request

from app.models.network import (
    ConfigMethod,
    GponChannel,
    IpProtocol,
    MgmtIpMode,
    OnuMode,
    SplitterPort,
    SplitterPortType,
    WanMode,
)
from app.schemas.network import OntUnitCreate, OntUnitUpdate
from app.services import network as network_service
from app.services import web_network_onts as web_onts_service
from app.services.audit_helpers import diff_dicts, log_audit_event, model_to_dict
from app.services.credential_crypto import encrypt_credential
from app.services.network.ont_provisioning_profiles import ont_provisioning_profiles


@dataclass
class OntFormResult:
    ont: Any | None = None
    form_model: Any | None = None
    error: str | None = None
    changes: dict[str, object] | None = None
    not_found: bool = False


def _actor_id_from_request(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    if not current_user:
        return None
    value = current_user.get("actor_id") or current_user.get("subscriber_id")
    return str(value) if value else None


def _log_ont_audit_event(
    db: Session,
    *,
    request: Request | None,
    action: str,
    ont_id: object,
    metadata: dict[str, object] | None,
) -> None:
    if request is None:
        return
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type="ont",
        entity_id=str(ont_id),
        actor_id=_actor_id_from_request(request),
        metadata=metadata,
    )


def form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def _normalize_iphost_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _iphost_value(config: dict[str, str], *patterns: str) -> str | None:
    if not config:
        return None
    normalized = {
        _normalize_iphost_key(key): str(value).strip()
        for key, value in config.items()
        if value is not None
    }
    for pattern in patterns:
        needle = _normalize_iphost_key(pattern)
        for key, value in normalized.items():
            if needle in key:
                return value
    return None


def initial_iphost_form(ont: Any, config: dict[str, str]) -> dict[str, str]:
    live_mode = (_iphost_value(config, "ip mode", "address mode", "mode") or "").lower()
    if "static" in live_mode:
        ip_mode = "static"
    elif "dhcp" in live_mode:
        ip_mode = "dhcp"
    elif (
        getattr(ont, "mgmt_ip_mode", None)
        and getattr(ont.mgmt_ip_mode, "value", None) == "static_ip"
    ):
        ip_mode = "static"
    else:
        ip_mode = "dhcp"

    live_vlan = _iphost_value(config, "vlan", "vlan id") or ""
    vlan_digits = re.search(r"\d+", live_vlan)
    live_ip = _iphost_value(config, "ip address", "ip") or ""
    subnet = _iphost_value(config, "subnet mask", "mask", "subnet") or ""
    gateway = _iphost_value(config, "gateway") or ""

    fallback_vlan = ""
    if getattr(ont, "mgmt_vlan", None) and getattr(ont.mgmt_vlan, "tag", None) is not None:
        fallback_vlan = str(ont.mgmt_vlan.tag)

    return {
        "ip_mode": ip_mode,
        "vlan_id": vlan_digits.group(0) if vlan_digits else fallback_vlan,
        "ip_address": live_ip or str(getattr(ont, "mgmt_ip_address", "") or ""),
        "subnet": subnet,
        "gateway": gateway,
    }


def form_uuid_or_none(form: FormData, key: str) -> uuid.UUID | None:
    value = form.get(key, "")
    raw = value if isinstance(value, str) else ""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def form_float_or_none(form: FormData, key: str) -> float | None:
    value = form.get(key, "")
    raw = value if isinstance(value, str) else ""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def form_int_or_none(form: FormData, key: str) -> int | None:
    value = form.get(key, "")
    raw = value if isinstance(value, str) else ""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def normalize_vendor_serial(value: str) -> str | None:
    normalized = "".join(ch for ch in value.upper() if ch.isalnum()).strip()
    return normalized or None


def resolve_splitter_port_id(
    db: Session,
    *,
    splitter_id: uuid.UUID | None,
    splitter_port_number: int | None,
) -> uuid.UUID | None:
    """Resolve a splitter/output port UUID from splitter and port number inputs."""
    if splitter_port_number is None:
        return None
    if splitter_id is None:
        raise ValueError("Select an ODB (Splitter) before setting an ODB Port.")

    stmt = (
        select(SplitterPort)
        .where(
            SplitterPort.splitter_id == splitter_id,
            SplitterPort.port_number == splitter_port_number,
            SplitterPort.port_type == SplitterPortType.output,
            SplitterPort.is_active.is_(True),
        )
        .limit(1)
    )
    splitter_port = db.scalars(stmt).first()
    if splitter_port is None:
        raise ValueError(
            f"ODB Port {splitter_port_number} was not found on the selected splitter."
        )
    return cast(uuid.UUID, splitter_port.id)


def ont_form_dependencies(db: Session, ont: Any | None = None) -> dict[str, object]:
    deps = web_onts_service.ont_form_dependencies(db, ont)
    deps["gpon_channels"] = [e.value for e in GponChannel]
    deps["onu_modes"] = [e.value for e in OnuMode]
    return deps


def ont_unit_integrity_error_message(exc: Exception) -> str:
    message = str(exc)
    if "uq_ont_units_serial_number" in message:
        return "Serial number already exists"
    return "ONT could not be saved due to a data conflict"


def build_ont_create_payload(form: FormData) -> tuple[OntUnitCreate | None, str | None]:
    serial_number = form_str(form, "serial_number").strip()
    if not serial_number:
        return None, "Serial number is required"
    payload = OntUnitCreate(
        serial_number=serial_number,
        vendor_serial_number=normalize_vendor_serial(
            form_str(form, "vendor_serial_number").strip()
        ),
        vendor=form_str(form, "vendor").strip() or None,
        model=form_str(form, "model").strip() or None,
        firmware_version=form_str(form, "firmware_version").strip() or None,
        notes=form_str(form, "notes").strip() or None,
        is_active=form_str(form, "is_active") == "true",
        onu_type_id=form_uuid_or_none(form, "onu_type_id"),
        olt_device_id=form_uuid_or_none(form, "olt_device_id"),
        pon_type=form_str(form, "pon_type").strip() or None,
        gpon_channel=form_str(form, "gpon_channel").strip() or None,
        board=form_str(form, "board").strip() or None,
        port=form_str(form, "port").strip() or None,
        onu_mode=form_str(form, "onu_mode").strip() or None,
        user_vlan_id=form_uuid_or_none(form, "user_vlan_id"),
        zone_id=form_uuid_or_none(form, "zone_id"),
        splitter_id=form_uuid_or_none(form, "splitter_id"),
        splitter_port_id=form_uuid_or_none(form, "splitter_port_id"),
        download_speed_profile_id=form_uuid_or_none(form, "download_speed_profile_id"),
        upload_speed_profile_id=form_uuid_or_none(form, "upload_speed_profile_id"),
        name=form_str(form, "name").strip() or None,
        address_or_comment=form_str(form, "address_or_comment").strip() or None,
        external_id=form_str(form, "external_id").strip() or None,
        use_gps=form_str(form, "use_gps") == "true",
        gps_latitude=form_float_or_none(form, "gps_latitude"),
        gps_longitude=form_float_or_none(form, "gps_longitude"),
    )
    if payload.is_active:
        return payload, "New ONTs must be inactive until assigned to a customer."
    return payload, None


def create_ont_from_form(
    db: Session, form: FormData, *, request: Request | None = None
) -> OntFormResult:
    payload, error = build_ont_create_payload(form)
    if error:
        return OntFormResult(form_model=payload, error=error)
    assert payload is not None
    try:
        ont = network_service.ont_units.create(db=db, payload=payload)
    except IntegrityError as exc:
        db.rollback()
        return OntFormResult(
            form_model=SimpleNamespace(**payload.model_dump()),
            error=ont_unit_integrity_error_message(exc),
        )
    _log_ont_audit_event(
        db,
        request=request,
        action="create",
        ont_id=ont.id,
        metadata={"serial_number": ont.serial_number},
    )
    return OntFormResult(ont=ont, form_model=payload)


def build_onu_mode_payload(form: FormData) -> OntUnitUpdate:
    return OntUnitUpdate(
        onu_mode=form_str(form, "onu_mode").strip() or None,
        wan_vlan_id=form_uuid_or_none(form, "wan_vlan_id"),
        wan_mode=form_str(form, "wan_mode").strip() or None,
        config_method=form_str(form, "config_method").strip() or None,
        ip_protocol=form_str(form, "ip_protocol").strip() or None,
        pppoe_username=form_str(form, "pppoe_username").strip() or None,
        pppoe_password=encrypt_credential(pw)
        if (pw := form_str(form, "pppoe_password").strip())
        else None,
        wan_remote_access=form_str(form, "wan_remote_access") == "true",
    )


def update_onu_mode_from_form(
    db: Session, ont_id: str, form: FormData, *, request: Request | None = None
) -> OntFormResult:
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return OntFormResult(not_found=True)
    payload = build_onu_mode_payload(form)
    before_snapshot = model_to_dict(ont)
    network_service.ont_units.update(db=db, unit_id=ont_id, payload=payload)
    after = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    changes = diff_dicts(before_snapshot, model_to_dict(after))
    metadata = {"changes": changes} if changes else None
    _log_ont_audit_event(
        db,
        request=request,
        action="update_onu_mode",
        ont_id=ont_id,
        metadata=metadata,
    )
    return OntFormResult(
        ont=after,
        form_model=after,
        changes=metadata,
    )


def build_location_address_or_comment(address: str, _contact: str) -> str | None:
    address_clean = address.strip()
    return address_clean or None


def location_form_values(form: FormData) -> dict[str, object]:
    return {
        "zone_id": form_uuid_or_none(form, "zone_id"),
        "splitter_id": form_uuid_or_none(form, "splitter_id"),
        "splitter_port_number": form_int_or_none(form, "splitter_port_number"),
        "name": form_str(form, "name").strip(),
        "address_or_comment": form_str(form, "address_or_comment").strip(),
        "contact": form_str(form, "contact").strip(),
        "gps_latitude": form_str(form, "gps_latitude").strip(),
        "gps_longitude": form_str(form, "gps_longitude").strip(),
    }


def update_location_details_from_form(
    db: Session, ont_id: str, form: FormData, *, request: Request | None = None
) -> OntFormResult:
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return OntFormResult(not_found=True)

    form_values = location_form_values(form)
    try:
        splitter_port_id = resolve_splitter_port_id(
            db,
            splitter_id=cast(uuid.UUID | None, form_values["splitter_id"]),
            splitter_port_number=cast(int | None, form_values["splitter_port_number"]),
        )
    except ValueError as exc:
        return OntFormResult(ont=ont, form_model=form_values, error=str(exc))

    gps_latitude = form_float_or_none(form, "gps_latitude")
    gps_longitude = form_float_or_none(form, "gps_longitude")
    address_val = (
        str(form_values["address_or_comment"])
        if form_values["address_or_comment"]
        else ""
    )
    contact_val = str(form_values["contact"]) if form_values["contact"] else ""
    payload = OntUnitUpdate(
        zone_id=cast(uuid.UUID | None, form_values["zone_id"]),
        splitter_id=cast(uuid.UUID | None, form_values["splitter_id"]),
        splitter_port_id=splitter_port_id,
        name=str(form_values["name"]) if form_values["name"] else None,
        address_or_comment=build_location_address_or_comment(address_val, contact_val),
        contact=contact_val or None,
        use_gps=gps_latitude is not None or gps_longitude is not None,
        gps_latitude=gps_latitude,
        gps_longitude=gps_longitude,
    )
    before_snapshot = model_to_dict(ont)
    network_service.ont_units.update(db=db, unit_id=ont_id, payload=payload)
    after = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    changes = diff_dicts(before_snapshot, model_to_dict(after))
    metadata = {"changes": changes} if changes else None
    _log_ont_audit_event(
        db,
        request=request,
        action="update_location_details",
        ont_id=ont_id,
        metadata=metadata,
    )
    return OntFormResult(
        ont=after,
        form_model=form_values,
        changes=metadata,
    )


def update_device_info_from_form(
    db: Session, ont_id: str, form: FormData, *, request: Request | None = None
) -> OntFormResult:
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return OntFormResult(not_found=True)

    form_values = {
        "vendor": getattr(ont, "vendor", None) or "",
        "model": getattr(ont, "model", None) or "",
        "firmware_version": getattr(ont, "firmware_version", None) or "",
        "onu_type_id": str(getattr(ont, "onu_type_id", "") or ""),
    }
    metadata = None
    _log_ont_audit_event(
        db,
        request=request,
        action="update_device_info",
        ont_id=ont_id,
        metadata=metadata,
    )
    return OntFormResult(
        ont=ont,
        form_model=form_values,
        changes=metadata,
    )


def update_gpon_channel_from_form(
    db: Session, ont_id: str, form: FormData, *, request: Request | None = None
) -> OntFormResult:
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return OntFormResult(not_found=True)
    payload = OntUnitUpdate(
        gpon_channel=form_str(form, "gpon_channel").strip() or "gpon"
    )
    before_snapshot = model_to_dict(ont)
    network_service.ont_units.update(db=db, unit_id=ont_id, payload=payload)
    after = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    changes = diff_dicts(before_snapshot, model_to_dict(after))
    metadata = {"changes": changes} if changes else None
    _log_ont_audit_event(
        db,
        request=request,
        action="update_gpon_channel",
        ont_id=ont_id,
        metadata=metadata,
    )
    return OntFormResult(
        ont=after,
        form_model=after,
        changes=metadata,
    )


def onu_mode_modal_context(db: Session, ont_id: str) -> dict[str, object]:
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    vlans = web_onts_service.get_vlans_for_ont(db, ont)
    return {
        "ont": ont,
        "vlans": vlans,
        "wan_modes": [e.value for e in WanMode],
        "config_methods": [e.value for e in ConfigMethod],
        "ip_protocols": [e.value for e in IpProtocol],
        "onu_modes": [e.value for e in OnuMode],
    }


def build_mgmt_ip_payload(form: FormData) -> OntUnitUpdate:
    return OntUnitUpdate(
        mgmt_ip_mode=form_str(form, "mgmt_ip_mode").strip() or None,
        mgmt_vlan_id=form_uuid_or_none(form, "mgmt_vlan_id"),
        mgmt_ip_address=form_str(form, "mgmt_ip_address").strip() or None,
        mgmt_remote_access=form_str(form, "mgmt_remote_access") == "true",
        voip_enabled=form_str(form, "voip_enabled") == "true",
    )


def update_mgmt_ip_from_form(
    db: Session, ont_id: str, form: FormData, *, request: Request | None = None
) -> OntFormResult:
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return OntFormResult(not_found=True)

    payload = build_mgmt_ip_payload(form)
    before_snapshot = model_to_dict(ont)
    network_service.ont_units.update(db=db, unit_id=ont_id, payload=payload)
    after = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    changes = diff_dicts(before_snapshot, model_to_dict(after))
    metadata = {"changes": changes} if changes else None
    _log_ont_audit_event(
        db,
        request=request,
        action="update_mgmt_ip",
        ont_id=ont_id,
        metadata=metadata,
    )
    return OntFormResult(ont=after, form_model=after, changes=metadata)


def mgmt_ip_modal_context(db: Session, ont_id: str) -> dict[str, object]:
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    vlans = web_onts_service.get_vlans_for_ont(db, ont)
    return {
        "ont": ont,
        "vlans": vlans,
        "mgmt_ip_modes": [e.value for e in MgmtIpMode],
    }


def profile_form_context(db: Session, ont_id: str) -> dict[str, object]:
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    available_profile_templates = ont_provisioning_profiles.list(
        db, is_active=True, limit=50
    )
    current_profile_id = (
        str(ont.provisioning_profile_id)
        if ont and ont.provisioning_profile_id
        else None
    )
    return {
        "ont_id": ont_id,
        "available_profile_templates": available_profile_templates,
        "current_profile_id": current_profile_id,
    }
