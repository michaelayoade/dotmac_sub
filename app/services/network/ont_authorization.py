"""ONT authorization service - OLT serial registration with DB state tracking.

Authorization runs synchronously because the OLT work is OMCI/CLI-driven. The
workflow registers the autofind serial and persists local inventory state before
returning. Follow-up service configuration is applied explicitly after
authorization.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.network import (
    OntAssignment,
    OntAuthorizationStatus,
    OntProvisioningStatus,
    OntUnit,
    OnuOnlineStatus,
)
from app.services.network._common import normalize_mac_address
from app.services.network.equipment_identity import normalize_ont_equipment_id
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.serial_utils import (
    normalize as normalize_serial,
)
from app.services.network.serial_utils import (
    normalized_serial_sql,
)
from app.services.network.serial_utils import (
    search_candidates as serial_search_candidates,
)

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)


@dataclass
class AuthorizationStepResult:
    """Result of one ONT authorization step."""

    step: int
    name: str
    success: bool
    message: str
    duration_ms: int = 0


@dataclass
class AuthorizationWorkflowResult:
    """Compatibility result shape for OLT authorization callers."""

    success: bool
    message: str
    steps: list[AuthorizationStepResult] = field(default_factory=list)
    ont_unit_id: str | None = None
    ont_id_on_olt: int | None = None
    status: str = "error"
    completed_authorization: bool = False
    partial_success: bool = False
    baseline_applied: bool | None = None
    duration_ms: int = 0

    @property
    def ont_id(self) -> str | None:
        """Backward-compatible alias for callers expecting an ONT unit ID."""
        return self.ont_unit_id

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "message": self.message,
            "ont_unit_id": self.ont_unit_id,
            "ont_id_on_olt": self.ont_id_on_olt,
            "status": self.status,
            "completed_authorization": self.completed_authorization,
            "partial_success": self.partial_success,
            "baseline_applied": self.baseline_applied,
            "duration_ms": self.duration_ms,
            "steps": [
                {
                    "step": step.step,
                    "name": step.name,
                    "success": step.success,
                    "message": step.message,
                    "duration_ms": step.duration_ms,
                }
                for step in self.steps
            ],
        }


def _is_serial_already_registered_message(message: str | None) -> bool:
    lowered = str(message or "").lower()
    return "sn already exists" in lowered or "serial already exists" in lowered


def _build_initial_ont_description(serial_number: str) -> str:
    """Default description applied at ``ont add`` time.

    The customer/service binding usually happens after authorization, so this is
    a stub that at minimum keeps the OLT row identifiable (no more
    ``ONT_NO_DESCRIPTION`` entries) and dates the authorization. Operators can
    override with ``ont modify ... desc`` later.
    """
    from datetime import UTC, datetime

    return f"{serial_number}_authd_{datetime.now(UTC).strftime('%Y%m%d')}"


def _validate_authorization_dependencies(
    db: Session,
    *,
    olt_id: str,
) -> str | None:
    """Return a blocking message when OLT profile dependencies are invalid."""
    from app.services.network.olt_dependency_preflight import (
        validate_olt_profile_dependencies,
    )

    result = validate_olt_profile_dependencies(
        db,
        olt_id=olt_id,
        operation="authorization",
    )
    if result.success:
        return None
    return result.message


def _serial_predicates(serial_number: str) -> list[str]:
    return [
        candidate
        for candidate in dict.fromkeys(
            normalize_serial(candidate)
            for candidate in serial_search_candidates(serial_number)
        )
        if candidate
    ]


def _get_or_create_active_assignment(db: Session, ont: OntUnit) -> OntAssignment:
    """Get the active assignment for an ONT, creating one if none exists."""
    from app.services import web_network_ont_assignments as assignments_service

    return assignments_service.get_or_create_active_assignment(db, ont)


def _commit_without_expiring(db: Session) -> None:
    """Commit before slow device I/O without forcing ORM reloads afterwards."""
    previous = db.expire_on_commit
    db.expire_on_commit = False
    try:
        db.commit()
    finally:
        db.expire_on_commit = previous


# ---------------------------------------------------------------------------
# Autofind candidate helpers
# ---------------------------------------------------------------------------


def get_autofind_candidate_by_serial(
    db: Session,
    olt_id: str,
    serial_number: str | None,
    *,
    fsp: str | None = None,
):
    """Return the active autofind candidate matching a serial on an OLT."""
    from app.models.ont_autofind import OltAutofindCandidate

    clean_serials = {
        normalize_serial(candidate)
        for candidate in serial_search_candidates(serial_number)
    }
    candidates = db.scalars(
        select(OltAutofindCandidate).where(
            OltAutofindCandidate.olt_id == olt_id,
            OltAutofindCandidate.is_active.is_(True),
        )
    ).all()
    clean_fsp = (fsp or "").strip()
    return next(
        (
            candidate
            for candidate in candidates
            if clean_serials.intersection(
                {
                    normalize_serial(value)
                    for serial in (candidate.serial_number, candidate.serial_hex)
                    for value in serial_search_candidates(serial)
                }
            )
            and (not clean_fsp or (candidate.fsp or "").strip() == clean_fsp)
        ),
        None,
    )


def _authorization_model_hint(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
) -> str | None:
    """Return the best known model before writing the ONT to the OLT."""
    clean_serials = _serial_predicates(serial_number)
    existing = db.scalars(
        select(OntUnit).where(
            normalized_serial_sql(OntUnit.serial_number).in_(clean_serials),
        )
    ).first()
    if existing and getattr(existing, "model", None):
        return normalize_ont_equipment_id(existing.model)

    candidate = get_autofind_candidate_by_serial(
        db,
        olt_id,
        serial_number,
        fsp=fsp,
    )
    candidate_model = getattr(candidate, "model", None)
    if candidate_model:
        return normalize_ont_equipment_id(candidate_model)

    candidate_ont = getattr(candidate, "ont_unit", None)
    if candidate_ont and getattr(candidate_ont, "model", None):
        return normalize_ont_equipment_id(candidate_ont.model)

    return None


def _resolve_authorized_autofind_candidate(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
) -> tuple[bool, str]:
    """Best-effort candidate cleanup after OLT authorization is verified."""
    from app.services import (
        web_network_ont_autofind as web_network_ont_autofind_service,
    )

    try:
        web_network_ont_autofind_service.resolve_candidate_authorized(
            db,
            olt_id=olt_id,
            fsp=fsp,
            serial_number=serial_number,
        )
        return True, "Marked the discovered ONT as authorized."
    except (SQLAlchemyError, ValueError) as exc:
        logger.warning(
            "Failed to resolve autofind candidate for %s on %s %s: %s",
            serial_number,
            olt_id,
            fsp,
            exc,
        )
        return True, "Authorization succeeded; autofind cleanup will run later."


# ---------------------------------------------------------------------------
# ONT record management
# ---------------------------------------------------------------------------


def create_or_find_ont_for_authorized_serial(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int | None = None,
    olt_run_state: str | None = None,
) -> tuple[str | None, str]:
    """Create or find an OntUnit for a just-authorized ONT serial."""
    from app.models.ont_autofind import OltAutofindCandidate
    from app.services.network.ont_status import (
        apply_resolved_status_for_model,
        set_authorization_status,
    )

    clean_serials = _serial_predicates(serial_number)
    olt = get_olt_or_none(db, olt_id)
    observed_olt_status = (
        OnuOnlineStatus.online
        if str(olt_run_state or "").strip().lower() == "online"
        else None
    )

    existing = db.scalars(
        select(OntUnit).where(
            normalized_serial_sql(OntUnit.serial_number).in_(clean_serials),
        )
    ).first()
    if existing:
        try:
            existing.olt_device_id = uuid.UUID(str(olt_id))
            existing.is_active = True
            set_authorization_status(
                existing, OntAuthorizationStatus.authorized, strict=False
            )
            if ont_id_on_olt is not None:
                existing.external_id = str(ont_id_on_olt)
            parts = fsp.split("/")
            if len(parts) == 3:
                existing.board = f"{parts[0]}/{parts[1]}"
                existing.port = parts[2]
            if observed_olt_status is not None:
                existing.olt_status = observed_olt_status
                existing.offline_reason = None
                existing.last_seen_at = datetime.now(UTC)
                existing.last_sync_source = "olt_authorization"
                existing.last_sync_at = datetime.now(UTC)
            apply_resolved_status_for_model(existing)
            db.flush()
            return (
                str(existing.id),
                f"Using existing ONT record {existing.serial_number}.",
            )
        except SQLAlchemyError as exc:
            db.rollback()
            return None, f"Failed to update existing ONT record: {exc}"

    candidates = db.scalars(
        select(OltAutofindCandidate).where(
            OltAutofindCandidate.olt_id == olt_id,
            OltAutofindCandidate.is_active.is_(True),
        )
    ).all()
    matched_candidate = next(
        (
            candidate
            for candidate in candidates
            if set(clean_serials).intersection(
                {
                    normalize_serial(value)
                    for serial in (candidate.serial_number, candidate.serial_hex)
                    for value in serial_search_candidates(serial)
                }
            )
        ),
        None,
    )

    display_serial = normalize_serial(serial_number)
    vendor = "Huawei" if display_serial.upper().startswith(("HWTC", "HWTT")) else None
    parts = fsp.split("/")
    board = f"{parts[0]}/{parts[1]}" if len(parts) == 3 else None
    port = parts[2] if len(parts) == 3 else None

    new_ont = OntUnit(
        id=uuid.uuid4(),
        serial_number=display_serial,
        external_id=str(ont_id_on_olt) if ont_id_on_olt is not None else None,
        vendor=vendor,
        model=getattr(matched_candidate, "model", None),
        mac_address=normalize_mac_address(getattr(matched_candidate, "mac", None)),
        olt_device_id=uuid.UUID(str(olt_id)),
        board=board,
        port=port,
        is_active=True,
        authorization_status=OntAuthorizationStatus.authorized,
        provisioning_status=OntProvisioningStatus.unprovisioned,
        olt_status=observed_olt_status or OnuOnlineStatus.offline,
        offline_reason=None,
        last_seen_at=datetime.now(UTC) if observed_olt_status else None,
        last_sync_source="olt_authorization" if observed_olt_status else None,
        last_sync_at=datetime.now(UTC) if observed_olt_status else None,
        pon_type="gpon",
        name=display_serial,
        desired_config={},
    )
    try:
        db.add(new_ont)
        from app.services.network.ont_status import apply_resolved_status_for_model

        apply_resolved_status_for_model(new_ont)
        db.flush()
    except SQLAlchemyError as exc:
        db.rollback()
        return None, f"Failed to create ONT record: {exc}"

    return str(new_ont.id), f"Created ONT record for {display_serial}."


def ensure_assignment_and_pon_port_for_authorized_ont(
    db: Session,
    *,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
) -> tuple[bool, str]:
    """Ensure the authorized ONT is linked to an active assignment and PON port."""
    from app.services.network.ont_assignment_alignment import (
        align_ont_assignment_to_authoritative_fsp,
    )

    ont = db.get(OntUnit, ont_unit_id)
    if ont is None:
        return False, "ONT record not found."

    try:
        result = align_ont_assignment_to_authoritative_fsp(
            db,
            ont=ont,
            olt_id=olt_id,
            fsp=fsp,
        )
        if result is None:
            return False, f"Invalid OLT F/S/P for assignment: {fsp}."
        db.flush()
        return True, f"Linked ONT to PON port {result.pon_port.name}."
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning(
            "Failed to link assignment/PON port for ONT %s on OLT %s %s",
            ont_unit_id,
            olt_id,
            fsp,
            exc_info=True,
        )
        message = str(exc).casefold()
        if "locknotavailable" in message or "lock timeout" in message:
            return False, (
                "The ONT was authorized, but the system was busy linking it to the "
                "PON port. Retry ONT reconcile."
            )
        return False, "Failed to link ONT to PON port. Check server logs."


# ---------------------------------------------------------------------------
# Core authorization
# ---------------------------------------------------------------------------


def authorize_autofind_ont(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool = False,
    preset_id: str | None = None,
) -> AuthorizationWorkflowResult:
    """Authorize an ONT on an OLT and persist ONT inventory state."""
    from app.services.network.olt_profile_resolution import (
        AuthorizationProfileResolution,
        resolve_authorization_profiles_from_import,
    )
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.olt_write_reconciliation import verify_ont_absent

    steps: list[AuthorizationStepResult] = []
    started_at = monotonic()

    def add_step(name: str, success: bool, message: str, step_started: float) -> None:
        steps.append(
            AuthorizationStepResult(
                step=len(steps) + 1,
                name=name,
                success=success,
                message=message,
                duration_ms=max(0, int((monotonic() - step_started) * 1000)),
            )
        )

    def finish(
        *,
        success: bool,
        message: str,
        status: str,
        ont_unit_id: str | None = None,
        ont_id_on_olt: int | None = None,
        completed_authorization: bool = False,
        partial_success: bool = False,
    ) -> AuthorizationWorkflowResult:
        return AuthorizationWorkflowResult(
            success=success,
            message=message,
            steps=steps,
            ont_unit_id=ont_unit_id,
            ont_id_on_olt=ont_id_on_olt,
            status=status,
            completed_authorization=completed_authorization,
            partial_success=partial_success,
            duration_ms=max(0, int((monotonic() - started_at) * 1000)),
        )

    olt = get_olt_or_none(db, olt_id)
    if olt is None:
        return finish(success=False, message="OLT not found", status="error")

    normalized_serial = normalize_serial(serial_number)

    dependency_error = _validate_authorization_dependencies(db, olt_id=str(olt.id))
    if dependency_error is not None:
        add_step(
            "Validate OLT Profile Dependencies", False, dependency_error, started_at
        )
        return finish(success=False, message=dependency_error, status="error")

    adapter = get_protocol_adapter(olt)
    _commit_without_expiring(db)

    # Handle force reauthorize - remove existing registration first
    if force_reauthorize:
        force_started = monotonic()
        find_result = adapter.find_ont_by_serial(normalized_serial)
        existing = find_result.data.get("registration") if find_result.success else None
        if not find_result.success:
            add_step("Activate ONT", False, find_result.message, force_started)
            return finish(success=False, message=find_result.message, status="error")
        if existing:
            delete_result = adapter.deauthorize_ont(existing.fsp, existing.onu_id)
            if not delete_result.success:
                add_step("Activate ONT", False, delete_result.message, force_started)
                return finish(
                    success=False, message=delete_result.message, status="error"
                )
            absence = verify_ont_absent(
                olt,
                fsp=existing.fsp,
                ont_id=existing.onu_id,
                serial_number=normalized_serial,
            )
            if not absence.success:
                add_step("Activate ONT", False, absence.message, force_started)
                return finish(success=False, message=absence.message, status="error")

    # Resolve authorization profiles
    activation_started = monotonic()
    authorization_profiles: AuthorizationProfileResolution | None = None

    model_hint = _authorization_model_hint(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=normalized_serial,
    )

    if authorization_profiles is None:
        profiles_ok, profiles_msg, authorization_profiles = (
            resolve_authorization_profiles_from_import(
                db,
                olt,
                equipment_id=model_hint,
            )
        )
        if not profiles_ok or authorization_profiles is None:
            add_step("Activate ONT", False, profiles_msg, activation_started)
            return finish(success=False, message=profiles_msg, status="error")

    # Authorize on OLT
    _commit_without_expiring(db)
    auth_description = _build_initial_ont_description(normalized_serial)
    auth_result = adapter.authorize_ont(
        fsp,
        normalized_serial,
        line_profile_id=authorization_profiles.line_profile_id,
        service_profile_id=authorization_profiles.service_profile_id,
        description=auth_description,
    )
    ont_id = auth_result.ont_id

    # Handle "serial already exists" case
    if not auth_result.success or ont_id is None:
        if _is_serial_already_registered_message(auth_result.message):
            find_result = adapter.find_ont_by_serial(normalized_serial)
            existing = (
                find_result.data.get("registration") if find_result.success else None
            )
            if (
                existing is not None
                and str(getattr(existing, "fsp", "")).strip() == fsp
            ):
                # Already on this port, reuse
                raw_ont_id = getattr(existing, "onu_id", None)
                ont_id = int(raw_ont_id) if raw_ont_id is not None else None
                add_step(
                    "Activate ONT",
                    True,
                    "ONT serial was already registered on the OLT; reusing registration.",
                    activation_started,
                )
            else:
                # On different port - remove and re-add
                if not find_result.success or existing is None:
                    msg = "ONT serial already exists, but existing registration not found."
                    add_step("Activate ONT", False, msg, activation_started)
                    return finish(success=False, message=msg, status="error")

                delete_result = adapter.deauthorize_ont(existing.fsp, existing.onu_id)
                if not delete_result.success:
                    add_step(
                        "Activate ONT", False, delete_result.message, activation_started
                    )
                    return finish(
                        success=False, message=delete_result.message, status="error"
                    )

                absence = verify_ont_absent(
                    olt,
                    fsp=existing.fsp,
                    ont_id=existing.onu_id,
                    serial_number=normalized_serial,
                )
                if not absence.success:
                    add_step("Activate ONT", False, absence.message, activation_started)
                    return finish(
                        success=False, message=absence.message, status="error"
                    )

                auth_result = adapter.authorize_ont(
                    fsp,
                    normalized_serial,
                    line_profile_id=authorization_profiles.line_profile_id,
                    service_profile_id=authorization_profiles.service_profile_id,
                    description=auth_description,
                )
                ont_id = auth_result.ont_id
                if not auth_result.success or ont_id is None:
                    msg = f"Removed old registration, but authorization failed: {auth_result.message}"
                    add_step("Activate ONT", False, msg, activation_started)
                    return finish(success=False, message=msg, status="error")
                auth_result.message = (
                    f"Removed existing ONT registration on {existing.fsp}; "
                    f"authorized on {fsp}."
                )
        else:
            message = auth_result.message or "Authorization failed"
            add_step("Activate ONT", False, message, activation_started)
            return finish(success=False, message=message, status="error")

    # Create/find ONT record
    ont_unit_id, create_msg = create_or_find_ont_for_authorized_serial(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=normalized_serial,
        ont_id_on_olt=ont_id,
    )
    if ont_unit_id is None:
        add_step("Activate ONT", False, create_msg, activation_started)
        return finish(
            success=False,
            message=(
                "ONT authorized on OLT, but local inventory record setup failed: "
                f"{create_msg}"
            ),
            status="error",
            ont_id_on_olt=ont_id,
            completed_authorization=True,
            partial_success=True,
        )

    assignment_ok, assignment_msg = ensure_assignment_and_pon_port_for_authorized_ont(
        db,
        ont_unit_id=ont_unit_id,
        olt_id=olt_id,
        fsp=fsp,
    )
    if not assignment_ok:
        add_step("Link ONT Assignment", False, assignment_msg, activation_started)
        return finish(
            success=False,
            message=(
                "ONT authorized on OLT, but local PON assignment setup failed: "
                f"{assignment_msg}"
            ),
            status="error",
            ont_unit_id=ont_unit_id,
            ont_id_on_olt=ont_id,
            completed_authorization=True,
            partial_success=True,
        )

    # Resolve autofind candidate
    _resolve_authorized_autofind_candidate(
        db, olt_id=olt_id, fsp=fsp, serial_number=normalized_serial
    )

    activation_message = (
        f"{getattr(authorization_profiles, 'message', '')} "
        f"{auth_result.message} {create_msg} {assignment_msg}".strip()
    )
    add_step("Activate ONT", True, activation_message, activation_started)

    return finish(
        success=True,
        message="ONT authorization completed.",
        status="success",
        ont_unit_id=ont_unit_id,
        ont_id_on_olt=ont_id,
        completed_authorization=True,
    )


def authorize_ont(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool = False,
    preset_id: str | None = None,
    request: Request | None = None,
    provision: bool = True,
) -> AuthorizationWorkflowResult:
    """Authorize ONT on the OLT, apply the OLT baseline, and audit log.

    This is the main entry point for ONT authorization. It:
    1. Registers the ONT serial on the OLT (line/service profiles)
    2. Creates/updates the OntUnit record
    3. Applies OLT-side internet and ACS reachability config
    4. Logs the action for audit

    After TR-069 binding, the ONT reboots and connects to ACS automatically.

    Args:
        db: Database session.
        olt_id: OLT device ID.
        fsp: Frame/Slot/Port (e.g., "0/1/0").
        serial_number: ONT serial number.
        force_reauthorize: Remove existing registration before authorizing.
        preset_id: Optional preset ID (unused, kept for compatibility).
        request: Optional request for audit logging.
        provision: If True, apply OLT baseline after authorization (default True).
    """
    from app.services.network.ont_provision_steps import apply_authorization_baseline

    started_at = monotonic()

    # Step 1: Core OLT authorization (register serial, create record, link PON)
    result = authorize_autofind_ont(
        db,
        olt_id,
        fsp,
        serial_number,
        force_reauthorize=force_reauthorize,
        preset_id=preset_id,
    )

    if not result.success:
        _audit_authorization(
            db, request, olt_id, fsp, serial_number, force_reauthorize, result
        )
        return result

    db.commit()

    # Step 2: Apply OLT baseline (internet service port + ACS reachability)
    if provision and result.ont_unit_id:
        provision_result = apply_authorization_baseline(db, result.ont_unit_id)
        if provision_result.success:
            result.baseline_applied = True
            result.steps.append(
                AuthorizationStepResult(
                    step=len(result.steps) + 1,
                    name="Apply Authorization Baseline",
                    success=True,
                    message=provision_result.message,
                    duration_ms=provision_result.duration_ms,
                )
            )
            db.commit()
        else:
            # Provisioning failed but authorization succeeded - partial success
            result.baseline_applied = False
            result.steps.append(
                AuthorizationStepResult(
                    step=len(result.steps) + 1,
                    name="Apply Authorization Baseline",
                    success=False,
                    message=provision_result.message,
                    duration_ms=provision_result.duration_ms,
                )
            )
            result.status = "warning"
            result.partial_success = True
            result.message = (
                "ONT authorized, but OLT service baseline failed: "
                f"{provision_result.message}"
            )

    _audit_authorization(
        db, request, olt_id, fsp, serial_number, force_reauthorize, result
    )
    result.duration_ms = max(0, int((monotonic() - started_at) * 1000))
    return result


def _audit_authorization(
    db: Session,
    request: Request | None,
    olt_id: str,
    fsp: str,
    serial_number: str,
    force_reauthorize: bool,
    result: AuthorizationWorkflowResult,
) -> None:
    """Log authorization action for audit trail."""
    from app.services.network.action_logging import log_network_action_result

    status = (
        "success"
        if result.success
        else ("warning" if result.partial_success else "error")
    )
    log_olt_audit_event(
        db,
        request=request,
        action="force_authorize_ont" if force_reauthorize else "authorize_ont",
        entity_id=olt_id,
        metadata={
            "result": status,
            "message": result.message,
            "fsp": fsp,
            "serial_number": serial_number,
            "force_reauthorize": force_reauthorize,
        },
        status_code=200 if result.success or result.partial_success else 500,
        is_success=result.success,
    )
    log_network_action_result(
        request=request,
        resource_type="olt",
        resource_id=olt_id,
        action="Force Authorize ONT" if force_reauthorize else "Authorize ONT",
        success=result.success,
        message=result.message,
        metadata={
            "fsp": fsp,
            "serial_number": serial_number,
            "force_reauthorize": force_reauthorize,
        },
    )
