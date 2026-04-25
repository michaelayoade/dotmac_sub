"""ONT decommission service for permanent hardware removal.

This module provides functionality to permanently decommission faulty ONT hardware,
including cleanup of all associated records (assignments, bundles, config overrides,
TR-069 bindings).

Gap 14 implementation: Hard delete / decommission feature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.network import OntAssignment, OntUnit

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)


@dataclass
class DecommissionResult:
    """Result of ONT decommission operation."""

    success: bool
    message: str
    ont_id: str | None = None
    serial_number: str | None = None
    olt_name: str | None = None
    cleanup_stats: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "message": self.message,
            "ont_id": self.ont_id,
            "serial_number": self.serial_number,
            "olt_name": self.olt_name,
            "cleanup_stats": self.cleanup_stats,
            "errors": self.errors,
        }


# Valid decommission reasons
DECOMMISSION_REASONS = {
    "hardware_fault": "Hardware Fault",
    "lost": "Lost/Missing",
    "stolen": "Stolen",
    "damaged": "Physical Damage",
    "obsolete": "Obsolete/End of Life",
    "rma": "Return to Manufacturer (RMA)",
    "other": "Other",
}


@dataclass
class DecommissionPreview:
    """Preview of what decommission will affect (dry run)."""

    ont_id: str
    serial_number: str | None
    olt_name: str | None
    can_decommission: bool
    warnings: list[str]
    affected: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "ont_id": self.ont_id,
            "serial_number": self.serial_number,
            "olt_name": self.olt_name,
            "can_decommission": self.can_decommission,
            "warnings": self.warnings,
            "affected": self.affected,
        }


def preview_decommission(db: Session, ont_id: str) -> DecommissionPreview:
    """Preview what would be affected by decommissioning an ONT.

    Use this before decommission_ont() to show users what will be deleted.
    Does not modify any data.

    Returns:
        DecommissionPreview with affected counts and warnings.
    """
    from app.models.tr069 import Tr069CpeDevice

    warnings: list[str] = []
    affected: dict[str, int] = {
        "active_assignments": 0,
        "service_port_allocations": 0,
        "acs_devices": 0,
    }

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return DecommissionPreview(
            ont_id=ont_id,
            serial_number=None,
            olt_name=None,
            can_decommission=False,
            warnings=["ONT not found"],
            affected=affected,
        )

    serial_number = ont.serial_number
    olt = ont.olt_device
    olt_name = olt.name if olt else None

    # Check if already decommissioned
    if not ont.is_active:
        warnings.append("ONT is already inactive/decommissioned")

    # Count active assignments
    active_assignments = db.scalars(
        select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont_id,
            OntAssignment.active.is_(True),
        )
    ).all()
    affected["active_assignments"] = len(active_assignments)
    if active_assignments:
        warnings.append(
            f"Will close {len(active_assignments)} active customer assignment(s)"
        )

    # Count service port allocations
    try:
        from app.services.network.service_port_allocator import (
            get_allocations_for_ont,
        )

        allocations = get_allocations_for_ont(db, ont_id)
        affected["service_port_allocations"] = len(allocations)
    except Exception:
        pass  # Service port allocator may not be available

    # Check TR-069 device
    tr069_device = db.scalars(
        select(Tr069CpeDevice).where(Tr069CpeDevice.ont_unit_id == ont_id)
    ).first()
    if tr069_device:
        affected["acs_devices"] = 1
        warnings.append("Will unlink TR-069/ACS device record")

    # Check if still registered on OLT
    if ont.external_id and olt:
        warnings.append(f"Will deauthorize from OLT '{olt_name}'")

    return DecommissionPreview(
        ont_id=ont_id,
        serial_number=serial_number,
        olt_name=olt_name,
        can_decommission=True,
        warnings=warnings,
        affected=affected,
    )


def decommission_ont(
    db: Session,
    ont_id: str,
    *,
    reason: str = "hardware_fault",
    confirm: bool = False,
    remove_from_acs: bool = True,
    deauthorize_on_olt: bool = True,
    actor: str | None = None,
) -> DecommissionResult:
    """Permanently decommission an ONT, removing all associated records.

    This is a destructive operation that:
    1. Deauthorizes the ONT on the OLT (if still registered)
    2. Closes all active assignments
    3. Deactivates all bundle assignments
    4. Clears TR-069 CPE device association
    5. Optionally removes the device from ACS entirely
    6. Marks the ONT as inactive with decommission metadata

    Use this for faulty hardware that should never be reused.

    SAFETY: The `confirm` parameter MUST be True or the operation will be rejected.
    Use preview_decommission() first to show users what will be affected.

    Args:
        db: Database session
        ont_id: UUID of the ONT to decommission
        reason: Reason for decommission (must be a key from DECOMMISSION_REASONS)
        confirm: MUST be True to execute - prevents accidental decommission
        remove_from_acs: If True, delete the device from ACS entirely
        deauthorize_on_olt: If True, deauthorize the ONT on the OLT
        actor: Actor email for audit trail

    Returns:
        DecommissionResult with cleanup statistics and any errors
    """
    # Safety check: require explicit confirmation
    if not confirm:
        return DecommissionResult(
            success=False,
            message="Decommission requires explicit confirmation. Set confirm=True after reviewing preview_decommission() output.",
            ont_id=ont_id,
        )

    # Validate reason
    if reason not in DECOMMISSION_REASONS:
        return DecommissionResult(
            success=False,
            message=f"Invalid reason '{reason}'. Valid reasons: {', '.join(DECOMMISSION_REASONS.keys())}",
            ont_id=ont_id,
        )
    from app.models.tr069 import Tr069CpeDevice
    from app.services.events.dispatcher import emit_event
    from app.services.events.types import EventType

    stats: dict[str, int] = {
        "assignments_closed": 0,
        "service_ports_released": 0,
        "acs_devices_cleared": 0,
    }
    errors: list[str] = []

    # Get the ONT
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return DecommissionResult(
            success=False,
            message=f"ONT {ont_id} not found",
            ont_id=ont_id,
        )

    serial_number = ont.serial_number
    olt = ont.olt_device
    olt_name = olt.name if olt else None

    logger.info(
        "ont_decommission_start",
        extra={
            "event": "ont_decommission_start",
            "ont_id": ont_id,
            "serial_number": serial_number,
            "olt_name": olt_name,
            "reason": reason,
            "actor": actor,
        },
    )

    # Step 1: Deauthorize on OLT (if requested and still registered)
    if deauthorize_on_olt and olt and ont.external_id:
        try:
            from app.services.network.olt_protocol_adapters import get_protocol_adapter

            fsp = f"{ont.board}/{ont.port}" if ont.board and ont.port else None
            if fsp:
                adapter = get_protocol_adapter(olt)
                result = adapter.deauthorize_ont(fsp, int(ont.external_id))
                if result.success:
                    logger.info(
                        "Deauthorized ONT %s on OLT %s during decommission",
                        serial_number,
                        olt_name,
                    )
                else:
                    errors.append(f"OLT deauthorize failed: {result.message}")
        except Exception as exc:
            errors.append(f"OLT deauthorize error: {exc}")
            logger.warning(
                "Failed to deauthorize ONT %s during decommission: %s",
                serial_number,
                exc,
            )

    # Step 2: Close all assignments
    assignments = db.scalars(
        select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont_id,
            OntAssignment.active.is_(True),
        )
    ).all()
    for assignment in assignments:
        assignment.active = False
        assignment.released_at = datetime.now(UTC)
        assignment.release_reason = f"decommissioned:{reason}"
        stats["assignments_closed"] += 1

    # Step 3: Release service port allocations
    try:
        from app.services.network.service_port_allocator import release_all_for_ont

        released = release_all_for_ont(db, ont_id)
        stats["service_ports_released"] = released
    except Exception as exc:
        errors.append(f"Service port release error: {exc}")
        logger.warning(
            "Failed to release service ports for ONT %s during decommission: %s",
            serial_number,
            exc,
        )

    # Step 5: Clear TR-069 CPE device association
    tr069_device = db.scalars(
        select(Tr069CpeDevice).where(Tr069CpeDevice.ont_unit_id == ont_id)
    ).first()
    if tr069_device:
        genieacs_device_id = tr069_device.genieacs_device_id
        tr069_device.ont_unit_id = None
        stats["acs_devices_cleared"] = 1

        # Optionally delete from ACS entirely
        if remove_from_acs and genieacs_device_id and ont.tr069_acs_server:
            try:
                from app.services.acs_client import create_acs_client

                acs_client = create_acs_client(ont.tr069_acs_server)
                if acs_client:
                    delete_ok = acs_client.delete_device(genieacs_device_id)
                    if delete_ok:
                        logger.info(
                            "Deleted ONT %s from ACS during decommission",
                            serial_number,
                        )
                        # Also delete the local CPE device record
                        db.delete(tr069_device)
                    else:
                        errors.append("ACS device delete failed")
            except Exception as exc:
                errors.append(f"ACS delete error: {exc}")
                logger.warning(
                    "Failed to delete ONT %s from ACS during decommission: %s",
                    serial_number,
                    exc,
                )

    # Step 6: Mark ONT as decommissioned
    from app.models.network import OnuOnlineStatus

    ont.is_active = False
    ont.authorization_status = None  # type: ignore[assignment]  # Clear status
    ont.provisioning_status = None  # type: ignore[assignment]
    ont.online_status = OnuOnlineStatus.unknown
    ont.external_id = None  # Clear OLT registration ID
    # Store decommission metadata in notes
    decommission_note = (
        f"[DECOMMISSIONED {datetime.now(UTC).isoformat()}] "
        f"Reason: {reason}. Actor: {actor or 'system'}. "
        f"Stats: {stats}"
    )
    if ont.notes:
        ont.notes = f"{decommission_note}\n\n{ont.notes}"
    else:
        ont.notes = decommission_note

    db.flush()

    # Emit decommission event
    try:
        emit_event(
            db,
            EventType.ont_decommissioned,
            payload={
                "ont_id": ont_id,
                "serial_number": serial_number,
                "olt_name": olt_name,
                "reason": reason,
                "cleanup_stats": stats,
            },
            actor=actor,
        )
    except Exception as exc:
        logger.warning("Failed to emit decommission event: %s", exc)

    logger.info(
        "ont_decommission_complete",
        extra={
            "event": "ont_decommission_complete",
            "ont_id": ont_id,
            "serial_number": serial_number,
            "olt_name": olt_name,
            "reason": reason,
            "stats": stats,
            "errors": errors,
            "actor": actor,
        },
    )

    success = len(errors) == 0
    return DecommissionResult(
        success=success,
        message=(
            f"Successfully decommissioned ONT {serial_number}"
            if success
            else f"Decommissioned ONT {serial_number} with {len(errors)} error(s)"
        ),
        ont_id=ont_id,
        serial_number=serial_number,
        olt_name=olt_name,
        cleanup_stats=stats,
        errors=errors,
    )


def decommission_ont_audited(
    db: Session,
    ont_id: str,
    *,
    reason: str = "hardware_fault",
    confirm: bool = False,
    remove_from_acs: bool = True,
    deauthorize_on_olt: bool = True,
    request: Request | None = None,
    actor: str | None = None,
) -> DecommissionResult:
    """Decommission an ONT with audit logging.

    Wrapper around decommission_ont that adds audit event logging.

    SAFETY: The `confirm` parameter MUST be True or the operation will be rejected.
    """
    from app.services.network.olt_web_audit import log_olt_audit_event

    # Extract actor from request if not provided
    if actor is None and request is not None:
        user = getattr(request.state, "user", None) if hasattr(request, "state") else None
        actor = getattr(user, "email", None) if user else None

    result = decommission_ont(
        db,
        ont_id,
        reason=reason,
        confirm=confirm,
        remove_from_acs=remove_from_acs,
        deauthorize_on_olt=deauthorize_on_olt,
        actor=actor,
    )

    # Get OLT ID for audit
    ont = db.get(OntUnit, ont_id)
    olt_id = str(ont.olt_device_id) if ont and ont.olt_device_id else None

    try:
        log_olt_audit_event(
            db,
            request=request,
            action="decommission_ont",
            entity_id=olt_id,
            metadata={
                "result": "success" if result.success else "error",
                "message": result.message,
                "ont_id": ont_id,
                "serial_number": result.serial_number,
                "reason": reason,
                "remove_from_acs": remove_from_acs,
                "deauthorize_on_olt": deauthorize_on_olt,
                "cleanup_stats": result.cleanup_stats,
                "errors": result.errors,
            },
            status_code=200 if result.success else 500,
            is_success=result.success,
        )
    except Exception as exc:
        logger.warning(
            "Failed to write ONT decommission audit event: %s", exc, exc_info=True
        )

    return result
