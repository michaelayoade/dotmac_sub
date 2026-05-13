"""ONT provisioning preflight checks."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntAuthorizationStatus,
    OntUnit,
)
from app.models.tr069 import Tr069CpeDevice
from app.services.common import coerce_uuid
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_provisioning.optical_budget import (
    validate_optical_budget,
)
from app.services.network.serial_utils import parse_ont_id_on_olt

_AUTHORIZED_SYNC_SOURCES = {
    "olt_ssh_authorize",
    "olt_ssh_readback",
    "zabbix_data_ingest",
}

_AUTHORIZATION_BLOCKER_NAMES = {
    "ONT exists",
    "OLT assigned",
    "OLT position (F/S/P)",
    "Authorization profiles",
    "OLT SSH credentials",
    "Active PON assignment",
    "ACS configuration",
    "OLT config pack",
}


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value) or "")


def _active_assignment(db: Session, ont: OntUnit) -> OntAssignment | None:
    return db.scalars(
        select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
    ).first()


def _has_authorized_inventory_evidence(ont: OntUnit) -> bool:
    """Return True when inventory strongly indicates OLT authorization."""
    if parse_ont_id_on_olt(getattr(ont, "external_id", None)) is None:
        return False
    if not getattr(ont, "board", None) or getattr(ont, "port", None) is None:
        return False
    return str(getattr(ont, "last_sync_source", "") or "") in _AUTHORIZED_SYNC_SOURCES


def ont_authorization_ready(ont: OntUnit) -> tuple[bool, str, str]:
    """Evaluate whether the ONT is ready for provisioning after authorization.

    Returns ``(ready, status, message)`` where status is ``ok``, ``warn``, or
    ``fail`` for direct use in preflight check dictionaries.
    """
    status = getattr(ont, "authorization_status", None)
    if status == OntAuthorizationStatus.authorized:
        return True, "ok", "Authorized on OLT"
    if _has_authorized_inventory_evidence(ont):
        return True, "warn", "Authorization inferred from OLT inventory"
    if status in {
        OntAuthorizationStatus.pending,
        OntAuthorizationStatus.deauthorized,
        OntAuthorizationStatus.failed,
    }:
        return (
            False,
            "fail",
            f"Authorization status is {_enum_value(status).replace('_', ' ') or 'not authorized'}",
        )
    return False, "fail", "Authorize the ONT on the OLT before provisioning"


def _has_acs_inform(db: Session, ont: OntUnit, acs_server_id: object | None) -> bool:
    if getattr(ont, "acs_last_inform_at", None) is not None:
        return True
    query = (
        select(Tr069CpeDevice.id)
        .where(Tr069CpeDevice.ont_unit_id == ont.id)
        .where(Tr069CpeDevice.is_active.is_(True))
        .where(Tr069CpeDevice.genieacs_device_id.isnot(None))
        .where(Tr069CpeDevice.last_inform_at.isnot(None))
    )
    if acs_server_id:
        try:
            query = query.where(
                Tr069CpeDevice.acs_server_id == coerce_uuid(acs_server_id)
            )
        except (TypeError, ValueError):
            query = query.where(Tr069CpeDevice.acs_server_id == acs_server_id)
    return db.scalars(query).first() is not None


def validate_prerequisites(
    db: Session,
    ont_id: str,
    *,
    ont: OntUnit | None = None,
    effective_config: dict | None = None,
) -> dict:
    """Check prerequisites before provisioning.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        ont: Optional pre-fetched ONT to avoid redundant lookup.
        effective_config: Optional pre-resolved config from resolve_effective_ont_config().
    """
    checks: list[dict] = []
    if ont is None:
        ont = db.get(OntUnit, coerce_uuid(ont_id))
    olt: OLTDevice | None = None

    if not ont:
        checks.append(
            {
                "name": "ONT exists",
                "status": "fail",
                "message": "ONT not found",
                "can_auto_fix": False,
            }
        )
        return {"ready": False, "checks": checks}
    checks.append(
        {
            "name": "ONT exists",
            "status": "ok",
            "message": f"{ont.serial_number} ({ont.vendor or ''} {ont.model or ''})",
            "can_auto_fix": False,
        }
    )

    if ont.olt_device_id:
        olt = db.get(OLTDevice, ont.olt_device_id)
        checks.append(
            {
                "name": "OLT assigned",
                "status": "ok" if olt else "fail",
                "message": olt.name if olt else "Assigned OLT record not found",
                "can_auto_fix": False,
            }
        )
    else:
        checks.append(
            {
                "name": "OLT assigned",
                "status": "fail",
                "message": "No OLT - assign ONT to an OLT first",
                "can_auto_fix": False,
            }
        )

    if ont.board and ont.port is not None:
        checks.append(
            {
                "name": "OLT position (F/S/P)",
                "status": "ok",
                "message": f"{ont.board}/{ont.port}",
                "can_auto_fix": False,
            }
        )
    else:
        checks.append(
            {
                "name": "OLT position (F/S/P)",
                "status": "fail",
                "message": "Board/port not set - discover from OLT or enter manually",
                "can_auto_fix": False,
            }
        )

    olt_ont_id = parse_ont_id_on_olt(ont.external_id)
    if olt_ont_id is not None:
        checks.append(
            {
                "name": "OLT ONT-ID",
                "status": "ok",
                "message": str(olt_ont_id),
                "can_auto_fix": False,
            }
        )
    else:
        checks.append(
            {
                "name": "OLT ONT-ID",
                "status": "fail",
                "message": "No usable ONT-ID - authorize or resync the ONT from the OLT",
                "can_auto_fix": False,
            }
        )

    authorization_ready, authorization_status, authorization_message = (
        ont_authorization_ready(ont)
    )
    checks.append(
        {
            "name": "OLT authorization",
            "status": authorization_status,
            "message": authorization_message,
            "can_auto_fix": False,
        }
    )

    # Use pre-resolved config if provided, otherwise resolve
    if effective_config is not None:
        resolved_config = effective_config
    else:
        resolved_config = resolve_effective_ont_config(db, ont, olt=olt)
    resolved_values = resolved_config.get("values", {})
    acs_server_id = resolved_values.get("tr069_acs_server_id")
    tr069_profile_id = resolved_values.get("tr069_olt_profile_id")
    mgmt_vlan = resolved_values.get("mgmt_vlan")
    missing_acs_config: list[str] = []
    if not acs_server_id:
        missing_acs_config.append("ACS server")
    if tr069_profile_id in (None, ""):
        missing_acs_config.append("OLT TR-069 profile")
    if mgmt_vlan in (None, ""):
        missing_acs_config.append("management VLAN")
    if missing_acs_config:
        checks.append(
            {
                "name": "ACS configuration",
                "status": "fail",
                "message": f"Missing {', '.join(missing_acs_config)}",
                "can_auto_fix": False,
            }
        )
    else:
        checks.append(
            {
                "name": "ACS configuration",
                "status": "ok",
                "message": f"ACS server {acs_server_id}, TR-069 profile {tr069_profile_id}, management VLAN {mgmt_vlan}",
                "can_auto_fix": False,
            }
        )

    if missing_acs_config:
        checks.append(
            {
                "name": "ACS connection",
                "status": "fail",
                "message": "Complete ACS configuration before provisioning",
                "can_auto_fix": False,
            }
        )
    elif _has_acs_inform(db, ont, acs_server_id):
        checks.append(
            {
                "name": "ACS connection",
                "status": "ok",
                "message": "ONT has informed ACS",
                "can_auto_fix": False,
            }
        )
    else:
        checks.append(
            {
                "name": "ACS connection",
                "status": "warn",
                "message": "ONT has not informed ACS yet; provisioning will bind TR-069 and wait for inform",
                "can_auto_fix": False,
            }
        )

    line_profile_id = resolved_values.get("authorization_line_profile_id")
    service_profile_id = resolved_values.get("authorization_service_profile_id")
    if line_profile_id is not None and service_profile_id is not None:
        checks.append(
            {
                "name": "Authorization profiles",
                "status": "ok",
                "message": f"Line {line_profile_id}, service {service_profile_id}",
                "can_auto_fix": False,
            }
        )
    else:
        checks.append(
            {
                "name": "Authorization profiles",
                "status": "fail",
                "message": "Import OLT state and add an ONT type profile mapping for this equipment ID",
                "can_auto_fix": False,
            }
        )

    if olt and olt.ssh_username and olt.ssh_password:
        checks.append(
            {
                "name": "OLT SSH credentials",
                "status": "ok",
                "message": f"User: {olt.ssh_username}",
                "can_auto_fix": False,
            }
        )
    elif ont.olt_device_id:
        checks.append(
            {
                "name": "OLT SSH credentials",
                "status": "fail",
                "message": "SSH not configured on OLT",
                "can_auto_fix": False,
            }
        )

    assignment = _active_assignment(db, ont)
    if assignment and assignment.pon_port_id:
        checks.append(
            {
                "name": "Active PON assignment",
                "status": "ok",
                "message": str(assignment.pon_port_id),
                "can_auto_fix": False,
            }
        )
    else:
        checks.append(
            {
                "name": "Active PON assignment",
                "status": "fail",
                "message": "Assign the ONT to a PON port before provisioning",
                "can_auto_fix": False,
            }
        )

    optical = validate_optical_budget(ont)
    if not optical.is_valid:
        optical_status = "fail"
    elif optical.is_warning or optical.rx_power_dbm is None:
        optical_status = "warn"
    else:
        optical_status = "ok"
    checks.append(
        {
            "name": "Optical signal",
            "status": optical_status,
            "message": optical.message,
            "can_auto_fix": False,
        }
    )

    # OLT config pack validation. Authorization profiles are resolved per ONT
    # equipment ID from imported OLT mappings, not from config_pack defaults.
    config_pack = resolved_config.get("config_pack")
    if config_pack and config_pack.is_complete:
        checks.append(
            {
                "name": "OLT config pack",
                "status": "ok",
                "message": f"TR-069 profile {config_pack.tr069_olt_profile_id}",
                "can_auto_fix": False,
            }
        )
    else:
        missing = []
        if config_pack:
            if not config_pack.has_vlans:
                missing.append("VLANs")
            if not config_pack.has_tr069_config:
                missing.append("TR-069")
        checks.append(
            {
                "name": "OLT config pack",
                "status": "fail",
                "message": f"OLT missing: {', '.join(missing) if missing else 'config pack'}",
                "can_auto_fix": False,
            }
        )

    for check in checks:
        check["blocks_authorization"] = check["name"] in _AUTHORIZATION_BLOCKER_NAMES

    ready_to_authorize = all(
        check["status"] != "fail" for check in checks if check["blocks_authorization"]
    )
    ready_to_provision = authorization_ready and all(
        check["status"] != "fail" for check in checks
    )
    return {
        "ready": ready_to_provision,
        "ready_to_authorize": ready_to_authorize,
        "ready_to_provision": ready_to_provision,
        "checks": checks,
    }
