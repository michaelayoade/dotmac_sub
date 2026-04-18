"""OLT lifecycle management service.

Handles OLT status transitions, deletion impact analysis, and status checks
for authorization workflows. Supports the drain state for controlled
OLT decommissioning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import (
    DeviceStatus,
    IpPool,
    OLTDevice,
    OntAssignment,
    OntProvisioningProfile,
    OntUnit,
    PonPort,
    Vlan,
)
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network.olt_inventory import get_olt_or_none

logger = logging.getLogger(__name__)


# Statuses that block new ONT authorizations
_BLOCKING_STATUSES = frozenset(
    {
        DeviceStatus.draining,
        DeviceStatus.retired,
        DeviceStatus.inactive,
    }
)


def is_olt_accepting_new_onts(olt: OLTDevice) -> tuple[bool, str]:
    """Check if OLT can accept new ONT authorizations.

    Args:
        olt: The OLT device to check.

    Returns:
        Tuple of (can_authorize, reason). Returns (True, "") if authorized,
        (False, reason) if blocked.

    Blocks if:
        - status is draining, retired, or inactive
        - is_active is False
    """
    if not getattr(olt, "is_active", True):
        return False, f"OLT '{olt.name}' is deactivated and cannot accept new ONTs."

    status = getattr(olt, "status", DeviceStatus.active)
    if status in _BLOCKING_STATUSES:
        status_label = status.value if status else "unknown"
        if status == DeviceStatus.draining:
            return (
                False,
                f"OLT '{olt.name}' is in draining mode. New ONT authorizations "
                "are blocked while existing service continues.",
            )
        if status == DeviceStatus.retired:
            return (
                False,
                f"OLT '{olt.name}' is retired and cannot accept new ONTs.",
            )
        return (
            False,
            f"OLT '{olt.name}' has status '{status_label}' and cannot accept new ONTs.",
        )

    return True, ""


@dataclass
class OltDeletionImpact:
    """Impact summary for OLT deletion."""

    olt_id: str
    olt_name: str
    active_onts: int
    active_assignments: int
    vlans_to_orphan: int
    ip_pools_to_orphan: int
    provisioning_profiles: int
    can_delete: bool
    blocking_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def get_deletion_impact(db: Session, olt_id: str) -> OltDeletionImpact | None:
    """Compute deletion impact summary for an OLT.

    Args:
        db: Database session.
        olt_id: UUID string of the OLT.

    Returns:
        OltDeletionImpact with counts and blocking status, or None if OLT not found.
    """
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return None

    # Count active ONTs linked to this OLT
    active_onts = (
        db.scalar(
            select(func.count(OntUnit.id))
            .where(OntUnit.olt_device_id == olt.id)
            .where(OntUnit.is_active.is_(True))
        )
        or 0
    )

    # Count active assignments on PON ports of this OLT
    active_assignments = (
        db.scalar(
            select(func.count(OntAssignment.id))
            .join(PonPort, OntAssignment.pon_port_id == PonPort.id)
            .where(PonPort.olt_id == olt.id)
            .where(OntAssignment.active.is_(True))
        )
        or 0
    )

    # Count VLANs that will be orphaned (SET NULL on delete)
    vlans_to_orphan = (
        db.scalar(
            select(func.count(Vlan.id))
            .where(Vlan.olt_device_id == olt.id)
            .where(Vlan.is_active.is_(True))
        )
        or 0
    )

    # Count IP pools that will be orphaned (SET NULL on delete)
    ip_pools_to_orphan = (
        db.scalar(
            select(func.count(IpPool.id))
            .where(IpPool.olt_device_id == olt.id)
            .where(IpPool.is_active.is_(True))
        )
        or 0
    )

    # Count provisioning profiles scoped to this OLT
    provisioning_profiles = (
        db.scalar(
            select(func.count(OntProvisioningProfile.id))
            .where(OntProvisioningProfile.olt_device_id == olt.id)
            .where(OntProvisioningProfile.is_active.is_(True))
        )
        or 0
    )

    blocking_reasons: list[str] = []
    warnings: list[str] = []

    if active_onts > 0:
        blocking_reasons.append(f"{active_onts} active ONT(s) are linked to this OLT")
    if active_assignments > 0:
        blocking_reasons.append(
            f"{active_assignments} active assignment(s) exist on this OLT's PON ports"
        )

    if vlans_to_orphan > 0:
        warnings.append(f"{vlans_to_orphan} VLAN(s) will lose their OLT association")
    if ip_pools_to_orphan > 0:
        warnings.append(
            f"{ip_pools_to_orphan} IP pool(s) will lose their OLT association"
        )
    if provisioning_profiles > 0:
        warnings.append(
            f"{provisioning_profiles} provisioning profile(s) are scoped to this OLT"
        )

    return OltDeletionImpact(
        olt_id=str(olt.id),
        olt_name=olt.name,
        active_onts=active_onts,
        active_assignments=active_assignments,
        vlans_to_orphan=vlans_to_orphan,
        ip_pools_to_orphan=ip_pools_to_orphan,
        provisioning_profiles=provisioning_profiles,
        can_delete=len(blocking_reasons) == 0,
        blocking_reasons=blocking_reasons,
        warnings=warnings,
    )


# Valid status transitions (from -> allowed targets)
_STATUS_TRANSITIONS: dict[DeviceStatus, frozenset[DeviceStatus]] = {
    DeviceStatus.active: frozenset(
        {
            DeviceStatus.inactive,
            DeviceStatus.maintenance,
            DeviceStatus.draining,
            DeviceStatus.retired,
        }
    ),
    DeviceStatus.inactive: frozenset(
        {
            DeviceStatus.active,
            DeviceStatus.maintenance,
            DeviceStatus.retired,
        }
    ),
    DeviceStatus.maintenance: frozenset(
        {
            DeviceStatus.active,
            DeviceStatus.inactive,
            DeviceStatus.draining,
            DeviceStatus.retired,
        }
    ),
    DeviceStatus.draining: frozenset(
        {
            DeviceStatus.active,
            DeviceStatus.retired,
        }
    ),
    DeviceStatus.retired: frozenset(
        {
            DeviceStatus.active,  # Allow reactivation if needed
        }
    ),
}


def set_status(
    db: Session,
    olt_id: str,
    status: DeviceStatus,
    *,
    actor: str = "system",
) -> tuple[bool, str]:
    """Change OLT status with validation and event emission.

    Args:
        db: Database session.
        olt_id: UUID string of the OLT.
        status: Target status.
        actor: Who triggered the change (for audit).

    Returns:
        Tuple of (success, message).
    """
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"

    current_status = getattr(olt, "status", DeviceStatus.active)
    if current_status == status:
        return True, f"OLT is already in '{status.value}' status"

    allowed_transitions = _STATUS_TRANSITIONS.get(current_status, frozenset())
    if status not in allowed_transitions:
        return (
            False,
            f"Cannot transition from '{current_status.value}' to '{status.value}'. "
            f"Allowed: {', '.join(s.value for s in allowed_transitions)}",
        )

    old_status = current_status.value
    olt.status = status
    db.flush()

    emit_event(
        db,
        EventType.olt_updated,
        {
            "olt_id": str(olt.id),
            "name": olt.name,
            "status_change": {
                "from": old_status,
                "to": status.value,
            },
        },
        actor=actor,
    )

    logger.info(
        "OLT %s status changed from %s to %s by %s",
        olt.name,
        old_status,
        status.value,
        actor,
    )

    return True, f"OLT status changed from '{old_status}' to '{status.value}'"


def set_draining(
    db: Session,
    olt_id: str,
    *,
    actor: str = "system",
) -> tuple[bool, str]:
    """Set OLT to draining status, blocking new ONT authorizations.

    Args:
        db: Database session.
        olt_id: UUID string of the OLT.
        actor: Who triggered the change.

    Returns:
        Tuple of (success, message).
    """
    return set_status(db, olt_id, DeviceStatus.draining, actor=actor)


def set_active(
    db: Session,
    olt_id: str,
    *,
    actor: str = "system",
) -> tuple[bool, str]:
    """Restore OLT to active status, allowing new ONT authorizations.

    Args:
        db: Database session.
        olt_id: UUID string of the OLT.
        actor: Who triggered the change.

    Returns:
        Tuple of (success, message).
    """
    return set_status(db, olt_id, DeviceStatus.active, actor=actor)
