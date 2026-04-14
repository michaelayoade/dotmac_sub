"""ONT provisioning preflight checks."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntAuthorizationStatus,
    OntProvisioningProfile,
    OntUnit,
)
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid
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
                "status": "ok",
                "message": olt.name if olt else str(ont.olt_device_id),
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
    else:
        checks.append(
            {
                "name": "Provisioning profile",
                "status": "fail",
                "message": "No profile - create one in Catalog -> Provisioning Profiles",
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

    if assignment and assignment.subscriber_id:
        subscriber = db.get(Subscriber, assignment.subscriber_id)
        subscriber_name = (
            f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
            if subscriber
            else str(assignment.subscriber_id)
        )
        checks.append(
            {
                "name": "Subscriber assigned",
                "status": "ok",
                "message": subscriber_name,
                "can_auto_fix": False,
            }
        )

        active_sub = db.scalars(
            select(Subscription).where(
                Subscription.subscriber_id == assignment.subscriber_id,
                Subscription.status == SubscriptionStatus.active,
            )
        ).first()
        if active_sub:
            checks.append(
                {
                    "name": "Active subscription",
                    "status": "ok",
                    "message": str(active_sub.id)[:8] + "...",
                    "can_auto_fix": False,
                }
            )
        else:
            checks.append(
                {
                    "name": "Active subscription",
                    "status": "warn",
                    "message": "No active subscription",
                    "can_auto_fix": False,
                }
            )
    else:
        checks.append(
            {
                "name": "Subscriber assigned",
                "status": "warn",
                "message": "No subscriber - provisioning will skip PPPoE",
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

    if not tr069_required:
        tr069_status = "ok"
        tr069_msg = "Not required"
    elif tr069_olt_profile_id is not None:
        tr069_status = "ok"
        tr069_msg = f"Profile ID {tr069_olt_profile_id}"
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

    ready = authorization_ready and all(check["status"] != "fail" for check in checks)
    return {"ready": ready, "checks": checks}
