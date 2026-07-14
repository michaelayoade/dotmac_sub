"""ONT decommission service for permanent hardware removal.

This module provides functionality to permanently decommission faulty ONT hardware,
including cleanup of all associated records (assignments, desired config,
TR-069 bindings).

Gap 14 implementation: Hard delete / decommission feature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
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
    3. Clears ONT desired config and WAN service state
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
    if not deauthorize_on_olt or not remove_from_acs:
        return DecommissionResult(
            success=False,
            message=(
                "Permanent decommission requires verified OLT and ACS cleanup. "
                "Use return-to-inventory for reusable hardware."
            ),
            ont_id=ont_id,
        )
    from app.models.tr069 import Tr069CpeDevice
    from app.services.events.dispatcher import emit_event
    from app.services.events.types import EventType
    from app.services.network.ont_inventory import (
        cleanup_acs_state_for_return,
        cleanup_olt_state_for_return,
        reset_ont_service_state,
    )

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

    # External state must converge before local SOT is changed. Both cleanup
    # helpers are idempotent and include device readback/reconciliation.
    external_completed: list[str] = []
    if deauthorize_on_olt and olt and ont.external_id:
        try:
            ok, completed, cleanup_errors = cleanup_olt_state_for_return(db, ont_id)
            external_completed.extend(completed)
            if not ok:
                details = "; ".join([*completed, *cleanup_errors])
                if completed:
                    db.rollback()
                    _record_decommission_compensation(
                        db,
                        ont=ont,
                        description=(
                            "OLT cleanup partially completed before verified "
                            "decommission cleanup stopped."
                        ),
                        error_message=details,
                    )
                return DecommissionResult(
                    success=False,
                    message=(
                        "Decommission stopped before local state changed because "
                        f"OLT cleanup was not verified: {details}"
                    ),
                    ont_id=ont_id,
                    serial_number=serial_number,
                    olt_name=olt_name,
                    cleanup_stats=stats,
                    errors=cleanup_errors,
                )
        except Exception as exc:
            logger.exception("Failed verified OLT cleanup for ONT %s", serial_number)
            db.rollback()
            _record_decommission_compensation(
                db,
                ont=ont,
                description=(
                    "OLT cleanup raised before its final verified result; device "
                    "state requires operator review."
                ),
                error_message=str(exc),
            )
            return DecommissionResult(
                success=False,
                message=(
                    "Decommission stopped before local state changed because "
                    f"OLT cleanup failed: {exc}"
                ),
                ont_id=ont_id,
                serial_number=serial_number,
                olt_name=olt_name,
                cleanup_stats=stats,
                errors=[f"OLT cleanup error: {exc}"],
            )

    if remove_from_acs:
        try:
            ok, completed, cleanup_errors = cleanup_acs_state_for_return(db, ont)
            external_completed.extend(completed)
            if not ok:
                details = "; ".join([*external_completed, *cleanup_errors])
                db.rollback()
                _record_decommission_compensation(
                    db,
                    ont=ont,
                    description=(
                        "OLT cleanup may have completed, but ACS cleanup failed "
                        "before local decommission state was committed."
                    ),
                    error_message=details,
                )
                return DecommissionResult(
                    success=False,
                    message=(
                        "Decommission stopped before local state changed because "
                        f"ACS cleanup was not verified: {details}"
                    ),
                    ont_id=ont_id,
                    serial_number=serial_number,
                    olt_name=olt_name,
                    cleanup_stats=stats,
                    errors=cleanup_errors,
                )
        except Exception as exc:
            logger.exception("Failed verified ACS cleanup for ONT %s", serial_number)
            if external_completed:
                db.rollback()
                _record_decommission_compensation(
                    db,
                    ont=ont,
                    description=(
                        "Device cleanup partially completed before ACS cleanup raised."
                    ),
                    error_message=str(exc),
                )
            return DecommissionResult(
                success=False,
                message=(
                    "Decommission stopped before local state changed because "
                    f"ACS cleanup failed: {exc}"
                ),
                ont_id=ont_id,
                serial_number=serial_number,
                olt_name=olt_name,
                cleanup_stats=stats,
                errors=[f"ACS cleanup error: {exc}"],
            )

    # Step 2: Close all assignments
    assignments = db.scalars(
        select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont_id,
            OntAssignment.active.is_(True),
        )
    ).all()
    released_subscriber_ids = {
        assignment.subscriber_id
        for assignment in assignments
        if assignment.subscriber_id is not None
    }
    released_subscription_ids = {
        assignment.subscription_id
        for assignment in assignments
        if assignment.subscription_id is not None
    }
    for assignment in assignments:
        assignment.active = False
        assignment.released_at = datetime.now(UTC)
        assignment.release_reason = f"decommissioned:{reason}"
        stats["assignments_closed"] += 1

    # Release PD only when this was the subscriber's final active ONT. A
    # subscriber may legitimately have multiple services/ONTs and must keep
    # the stable delegated prefix while any one of them remains active.
    try:
        from app.services.ipv6_pd import (
            release_subscriber_prefixes,
            release_subscription_prefixes,
        )

        pd_released = sum(
            release_subscription_prefixes(db, subscription_id)
            for subscription_id in released_subscription_ids
        )
        # Legacy assignments without an explicit subscription can only be
        # released safely when no other active ONT exists for the subscriber.
        for subscriber_id in released_subscriber_ids:
            has_bound_assignment = any(
                assignment.subscriber_id == subscriber_id
                and assignment.subscription_id is not None
                for assignment in assignments
            )
            if has_bound_assignment:
                continue
            other_active = db.scalars(
                select(OntAssignment.id).where(
                    OntAssignment.subscriber_id == subscriber_id,
                    OntAssignment.active.is_(True),
                    OntAssignment.ont_unit_id != ont.id,
                )
            ).first()
            if other_active is None:
                pd_released += release_subscriber_prefixes(db, subscriber_id)
        stats["ipv6_prefixes_released"] = pd_released
    except Exception as exc:
        errors.append(f"IPv6 PD release error: {exc}")
        logger.warning("Failed to release IPv6 PD for ONT %s: %s", serial_number, exc)

    # Management addresses are IPAM resources, not ONT inventory attributes.
    # Release them after assignments are closed so the allocator no longer
    # protects the address as belonging to an active assignment.
    try:
        from app.services.network.ont_management_ipam import (
            release_ont_management_ip,
        )

        stats["management_ips_released"] = len(
            release_ont_management_ip(db, ont=ont, mode="inactive")
        )
    except Exception as exc:
        errors.append(f"Management IP release error: {exc}")
        logger.warning(
            "Failed to release management IP for ONT %s: %s",
            serial_number,
            exc,
        )

    # Release any remaining local service-port allocations. Verified OLT cleanup
    # already releases these, so this is intentionally idempotent.
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

    # Clear the local TR-069 association only after ACS cleanup has converged.
    tr069_devices = db.scalars(
        select(Tr069CpeDevice).where(Tr069CpeDevice.ont_unit_id == ont_id)
    ).all()
    for tr069_device in tr069_devices:
        if remove_from_acs:
            db.delete(tr069_device)
        else:
            tr069_device.ont_unit_id = None
        stats["acs_devices_cleared"] += 1

    # Step 6: Mark ONT as decommissioned
    from app.models.network import OnuOnlineStatus
    from app.services.network.ont_status import (
        clear_authorization_status,
        clear_provisioning_status,
    )

    # Inventory reuse must forget prior observations, but a permanently retired
    # asset keeps its last readback as forensic evidence.
    tr069_snapshot = ont.tr069_last_snapshot
    tr069_snapshot_at = ont.tr069_last_snapshot_at
    olt_snapshot = ont.olt_observed_snapshot
    olt_snapshot_at = ont.olt_observed_snapshot_at
    reset_ont_service_state(db, ont, reason="decommission")
    ont.tr069_last_snapshot = tr069_snapshot
    ont.tr069_last_snapshot_at = tr069_snapshot_at
    ont.olt_observed_snapshot = olt_snapshot
    ont.olt_observed_snapshot_at = olt_snapshot_at
    ont.is_active = False
    clear_authorization_status(ont, reason="decommission")
    clear_provisioning_status(ont, reason="decommission")
    ont.olt_status = OnuOnlineStatus.offline
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

    if errors:
        _record_decommission_compensation(
            db,
            ont=ont,
            description=(
                "ONT device cleanup and decommission completed, but one or more "
                "local resource releases require operator review."
            ),
            error_message="; ".join(errors),
            commit=False,
        )
    success = True
    return DecommissionResult(
        success=success,
        message=(
            f"Successfully decommissioned ONT {serial_number}"
            if not errors
            else (
                f"Decommissioned ONT {serial_number}; {len(errors)} local cleanup "
                "item(s) require review"
            )
        ),
        ont_id=ont_id,
        serial_number=serial_number,
        olt_name=olt_name,
        cleanup_stats=stats,
        errors=errors,
    )


def _record_decommission_compensation(
    db: Session,
    *,
    ont: OntUnit,
    description: str,
    error_message: str,
    commit: bool = True,
) -> None:
    """Persist forward-recovery evidence after a destructive partial result."""
    from app.models.compensation_failure import CompensationFailure

    failure = CompensationFailure(
        ont_unit_id=ont.id,
        olt_device_id=ont.olt_device_id,
        operation_type="ont_decommission",
        step_name="manual_decommission_cleanup_review",
        undo_commands=[],
        description=description,
        resource_id=str(ont.id),
        interface_path=(f"{ont.board}/{ont.port}" if ont.board and ont.port else None),
        error_message=error_message,
    )
    db.add(failure)
    if commit:
        db.commit()


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
    Commits on success, rollbacks on failure.

    SAFETY: The `confirm` parameter MUST be True or the operation will be rejected.
    """
    from fastapi import HTTPException

    from app.models.network_operation import (
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network.olt_web_audit import log_olt_audit_event
    from app.services.network_operations import network_operations

    # Extract actor from request if not provided
    if actor is None and request is not None:
        user = (
            getattr(request.state, "user", None) if hasattr(request, "state") else None
        )
        actor = getattr(user, "email", None) if user else None

    try:
        operation = network_operations.start(
            db,
            NetworkOperationType.ont_decommission,
            NetworkOperationTargetType.ont,
            ont_id,
            correlation_key=f"ont_decommission:{ont_id}",
            input_payload={
                "reason": reason,
                "remove_from_acs": remove_from_acs,
                "deauthorize_on_olt": deauthorize_on_olt,
            },
            initiated_by=actor,
        )
        network_operations.mark_running(db, str(operation.id))
        db.commit()
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        return DecommissionResult(
            success=False,
            message="A decommission operation is already in progress for this ONT.",
            ont_id=ont_id,
        )

    try:
        result = decommission_ont(
            db,
            ont_id,
            reason=reason,
            confirm=confirm,
            remove_from_acs=remove_from_acs,
            deauthorize_on_olt=deauthorize_on_olt,
            actor=actor,
        )
    except Exception as exc:
        db.rollback()
        network_operations.mark_failed(
            db,
            str(operation.id),
            f"Unexpected decommission failure: {exc}",
        )
        db.commit()
        raise

    output_payload = result.to_dict()
    if result.success:
        if result.errors:
            network_operations.mark_warning(
                db,
                str(operation.id),
                "; ".join(result.errors),
                output_payload=output_payload,
            )
        else:
            network_operations.mark_succeeded(
                db,
                str(operation.id),
                output_payload=output_payload,
            )
        db.commit()
    else:
        db.rollback()
        network_operations.mark_failed(
            db,
            str(operation.id),
            result.message,
            output_payload=output_payload,
        )
        db.commit()

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
