"""ONT provisioning preflight checks."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntAuthorizationStatus,
    OntProvisioningProfile,
    OntUnit,
)
from app.services.common import coerce_uuid
from app.services.network.ont_provisioning.optical_budget import (
    validate_optical_budget,
)
from app.services.network.ont_provisioning.profiles import (
    profile_requires_tr069,
    resolve_profile,
)
from app.services.network.serial_utils import parse_ont_id_on_olt


_AUTHORIZED_SYNC_SOURCES = {
    "olt_ssh_authorize",
    "olt_ssh_readback",
    "olt_snmp_sync",
    "olt_snmp_targeted",
    "olt_polling",
}

_AUTHORIZATION_BLOCKER_NAMES = {
    "ONT exists",
    "OLT assigned",
    "OLT position (F/S/P)",
    "Provisioning profile",
    "Authorization profiles",
    "OLT SSH credentials",
    "Active PON assignment",
    "TR-069 OLT profile",
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
    """Return True when legacy inventory strongly indicates OLT authorization."""
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
        return True, "warn", "Authorization inferred from OLT inventory sync"
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


def validate_prerequisites(
    db: Session,
    ont_id: str,
    *,
    profile_id: str | None = None,
    tr069_olt_profile_id: int | None = None,
) -> dict:
    """Check prerequisites before provisioning."""
    checks: list[dict] = []
    ont = db.get(OntUnit, coerce_uuid(ont_id))
    olt: OLTDevice | None = None
    profile: OntProvisioningProfile | None = None

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

    profile = resolve_profile(db, ont, profile_id)
    if profile:
        checks.append(
            {
                "name": "Provisioning profile",
                "status": "ok",
                "message": profile.name,
                "can_auto_fix": False,
            }
        )
        if (
            profile.authorization_line_profile_id is not None
            and profile.authorization_service_profile_id is not None
        ):
            checks.append(
                {
                    "name": "Authorization profiles",
                    "status": "ok",
                    "message": (
                        f"Line {profile.authorization_line_profile_id}, "
                        f"service {profile.authorization_service_profile_id}"
                    ),
                    "can_auto_fix": False,
                }
            )
        else:
            checks.append(
                {
                    "name": "Authorization profiles",
                    "status": "fail",
                    "message": (
                        "Set OLT line/service profile IDs before authorizing ONTs"
                    ),
                    "can_auto_fix": False,
                }
            )
    else:
        checks.append(
            {
                "name": "Provisioning profile",
                "status": "fail",
                "message": "No profile - create one in Catalog -> Provisioning Profiles",
                "can_auto_fix": False,
            }
        )
        checks.append(
            {
                "name": "Authorization profiles",
                "status": "fail",
                "message": "No provisioning profile selected for authorization",
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

    acs_server_id = ont.tr069_acs_server_id
    if not acs_server_id and olt is not None:
        acs_server_id = getattr(olt, "tr069_acs_server_id", None)
    if acs_server_id:
        checks.append(
            {
                "name": "TR-069 ACS server",
                "status": "ok",
                "message": "Configured",
                "can_auto_fix": False,
            }
        )
    else:
        checks.append(
            {
                "name": "TR-069 ACS server",
                "status": "warn",
                "message": "Not configured - TR-069 steps will be skipped",
                "can_auto_fix": False,
            }
        )

    profile_requires = profile_requires_tr069(profile)
    acs_enabled = bool(
        getattr(ont, "tr069_acs_server_id", None)
        or getattr(olt, "tr069_acs_server_id", None)
    )
    tr069_required = profile_requires or acs_enabled

    effective_tr069_profile_id = tr069_olt_profile_id
    if effective_tr069_profile_id is None:
        effective_tr069_profile_id = getattr(ont, "tr069_olt_profile_id", None)

    if not tr069_required:
        tr069_status = "ok"
        tr069_msg = "Not required"
    elif effective_tr069_profile_id is not None:
        tr069_status = "ok"
        tr069_msg = f"Profile ID {effective_tr069_profile_id}"
    elif acs_enabled:
        tr069_status = "ok"
        tr069_msg = "Profile will be resolved dynamically at provisioning time"
    elif profile_requires and not acs_enabled:
        tr069_status = "fail"
        tr069_msg = (
            "Selected provisioning profile requires TR-069, but no ACS-enabled OLT "
            "or ONT is configured."
        )
    else:
        tr069_status = "fail"
        tr069_msg = "Resolve or create an OLT TR-069 profile before provisioning."
    checks.append(
        {
            "name": "TR-069 OLT profile",
            "status": tr069_status,
            "message": tr069_msg,
            "can_auto_fix": False,
        }
    )

    for check in checks:
        check["blocks_authorization"] = check["name"] in _AUTHORIZATION_BLOCKER_NAMES

    ready_to_authorize = all(
        check["status"] != "fail"
        for check in checks
        if check["blocks_authorization"]
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
