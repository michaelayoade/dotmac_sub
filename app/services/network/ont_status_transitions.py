"""State transition guards for ONT authorization/provisioning statuses."""

from __future__ import annotations

import logging

from app.models.network import OntAuthorizationStatus, OntProvisioningStatus, OntUnit

logger = logging.getLogger(__name__)

_AUTHORIZATION_TRANSITIONS: dict[
    OntAuthorizationStatus | None, set[OntAuthorizationStatus]
] = {
    None: {
        OntAuthorizationStatus.pending,
        OntAuthorizationStatus.authorized,
        OntAuthorizationStatus.failed,
    },
    OntAuthorizationStatus.pending: {
        OntAuthorizationStatus.authorized,
        OntAuthorizationStatus.deauthorized,
        OntAuthorizationStatus.failed,
    },
    OntAuthorizationStatus.authorized: {
        OntAuthorizationStatus.pending,
        OntAuthorizationStatus.deauthorized,
        OntAuthorizationStatus.failed,
    },
    OntAuthorizationStatus.deauthorized: {
        OntAuthorizationStatus.pending,
        OntAuthorizationStatus.authorized,
        OntAuthorizationStatus.failed,
    },
    OntAuthorizationStatus.failed: {
        OntAuthorizationStatus.pending,
        OntAuthorizationStatus.authorized,
        OntAuthorizationStatus.deauthorized,
    },
}

_PROVISIONING_TRANSITIONS: dict[
    OntProvisioningStatus | None, set[OntProvisioningStatus]
] = {
    None: {
        OntProvisioningStatus.unprovisioned,
        OntProvisioningStatus.partial,
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.failed,
    },
    OntProvisioningStatus.unprovisioned: {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.failed,
        OntProvisioningStatus.drift_detected,
    },
    OntProvisioningStatus.partial: {
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.failed,
        OntProvisioningStatus.drift_detected,
        OntProvisioningStatus.unprovisioned,
    },
    OntProvisioningStatus.provisioned: {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.drift_detected,
        OntProvisioningStatus.failed,
        OntProvisioningStatus.unprovisioned,
    },
    OntProvisioningStatus.drift_detected: {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.failed,
        OntProvisioningStatus.unprovisioned,
    },
    OntProvisioningStatus.failed: {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.unprovisioned,
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.drift_detected,
    },
}


def _coerce_auth_status(
    status: OntAuthorizationStatus | str,
) -> OntAuthorizationStatus:
    if isinstance(status, OntAuthorizationStatus):
        return status
    return OntAuthorizationStatus(str(status))


def _coerce_provisioning_status(
    status: OntProvisioningStatus | str,
) -> OntProvisioningStatus:
    if isinstance(status, OntProvisioningStatus):
        return status
    return OntProvisioningStatus(str(status))


def set_authorization_status(
    ont: OntUnit,
    status: OntAuthorizationStatus | str,
    *,
    strict: bool = True,
) -> None:
    next_status = _coerce_auth_status(status)
    current = ont.authorization_status
    if current == next_status:
        return
    allowed = _AUTHORIZATION_TRANSITIONS.get(current, set())
    is_valid_transition = next_status in allowed
    if not is_valid_transition:
        message = (
            f"Illegal ONT authorization status transition: "
            f"{current.value if current else 'none'} -> {next_status.value}"
        )
        if strict:
            raise ValueError(message)
        logger.warning(message)
    # Log transition for audit trail
    logger.info(
        "ont_status_transition",
        extra={
            "event": "ont_status_transition",
            "ont_id": str(ont.id),
            "field": "authorization_status",
            "from": current.value if current else None,
            "to": next_status.value,
            "valid": is_valid_transition,
        },
    )
    ont.authorization_status = next_status


def set_provisioning_status(
    ont: OntUnit,
    status: OntProvisioningStatus | str,
    *,
    strict: bool = True,
) -> None:
    next_status = _coerce_provisioning_status(status)
    current = ont.provisioning_status
    if current == next_status:
        return
    allowed = _PROVISIONING_TRANSITIONS.get(current, set())
    is_valid_transition = next_status in allowed
    if not is_valid_transition:
        message = (
            f"Illegal ONT provisioning status transition: "
            f"{current.value if current else 'none'} -> {next_status.value}"
        )
        if strict:
            raise ValueError(message)
        logger.warning(message)
    # Log transition for audit trail
    logger.info(
        "ont_status_transition",
        extra={
            "event": "ont_status_transition",
            "ont_id": str(ont.id),
            "field": "provisioning_status",
            "from": current.value if current else None,
            "to": next_status.value,
            "valid": is_valid_transition,
        },
    )
    ont.provisioning_status = next_status
