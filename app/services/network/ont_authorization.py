"""ONT authorization service — OLT serial registration with DB state tracking.

This module handles the single atomic action of registering (or removing)
an ONT serial on an OLT port. It wraps the raw SSH functions with DB
updates to ``OntUnit.authorization_status``.

Authorization is decoupled from provisioning: authorizing an ONT registers
it on the OLT and assigns an ONT-ID, but does NOT configure service-ports,
management IP, TR-069, or PPPoE. Those are provisioning steps the operator
triggers separately.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    OntAuthorizationStatus,
    OntProvisioningStatus,
    OntUnit,
    OnuOnlineStatus,
)
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.ont_provisioning.context import resolve_olt_context
from app.services.network.ont_provisioning.result import StepResult
from app.services.network.ont_status_transitions import (
    set_authorization_status,
    set_provisioning_status,
)
from app.services.network.serial_utils import normalize as normalize_serial
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
    follow_up_operation_id: str | None = None
    duration_ms: int = 0
    pending_rediscovery: bool = False
    rediscovery_task_id: str | None = None

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
            "follow_up_operation_id": self.follow_up_operation_id,
            "duration_ms": self.duration_ms,
            "pending_rediscovery": self.pending_rediscovery,
            "rediscovery_task_id": self.rediscovery_task_id,
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


def _serial_predicates(serial_number: str) -> list[str]:
    return [
        candidate
        for candidate in dict.fromkeys(
            normalize_serial(candidate)
            for candidate in serial_search_candidates(serial_number)
        )
        if candidate
    ]


def refresh_pool_availability(
    db: Session,
    pool_id: str | uuid.UUID,
) -> tuple[str | None, int]:
    """Recompute next available IPv4 address and available count for a pool."""
    import ipaddress

    from app.models.network import IpBlock, IpPool, IPv4Address

    pool = db.get(IpPool, pool_id)
    if pool is None:
        return None, 0

    blocks = list(
        db.scalars(
            select(IpBlock)
            .where(IpBlock.pool_id == pool.id)
            .where(IpBlock.is_active.is_(True))
        ).all()
    )
    used = {
        str(address)
        for address in db.scalars(
            select(IPv4Address.address).where(IPv4Address.pool_id == pool.id)
        ).all()
    }
    gateway = getattr(pool, "gateway", None)
    if gateway:
        used.add(str(gateway))

    next_available: str | None = None
    available_count = 0
    for block in blocks:
        try:
            network = ipaddress.ip_network(str(block.cidr), strict=False)
        except ValueError:
            continue
        if network.version != 4:
            continue
        for ip_address in network.hosts():
            candidate = str(ip_address)
            if candidate in used:
                continue
            available_count += 1
            if next_available is None:
                next_available = candidate

    pool.next_available_ip = next_available
    pool.available_count = available_count
    db.flush()
    return next_available, available_count


def allocate_management_ip_for_ont(
    db: Session,
    *,
    ont_unit_id: str,
    olt_id: str,
) -> tuple[bool, str, str | None]:
    """Allocate a management IP from the OLT's management IP pool for the ONT.

    Returns:
        Tuple of (success, message, allocated_ip).
    """
    from app.models.network import IPv4Address, MgmtIpMode, OLTDevice

    ont = db.get(OntUnit, ont_unit_id)
    if ont is None:
        return False, "ONT not found.", None

    # Check if ONT already has a management IP
    if ont.mgmt_ip_address:
        return True, f"ONT already has management IP {ont.mgmt_ip_address}.", ont.mgmt_ip_address

    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return False, "OLT not found.", None

    # Get management IP pool from OLT config pack
    pool_id = olt.mgmt_ip_pool_id
    if not pool_id:
        logger.info(
            "No management IP pool configured for OLT %s, skipping IP allocation for ONT %s",
            olt.name,
            ont.serial_number,
        )
        return True, "No management IP pool configured on OLT.", None

    # Get next available IP from pool
    next_ip, available_count = refresh_pool_availability(db, pool_id)
    if not next_ip:
        logger.warning(
            "Management IP pool %s exhausted for OLT %s, cannot allocate IP for ONT %s",
            pool_id,
            olt.name,
            ont.serial_number,
        )
        return False, "Management IP pool exhausted.", None

    # Create IPv4Address record to reserve the IP
    note = f"ont:{ont_unit_id}"
    record = db.query(IPv4Address).filter(IPv4Address.address == next_ip).first()
    if record is None:
        record = IPv4Address(
            address=next_ip,
            pool_id=pool_id,
            is_reserved=True,
            notes=note,
            ont_unit_id=uuid.UUID(ont_unit_id),
        )
        db.add(record)
    else:
        record.is_reserved = True
        record.notes = note
        record.ont_unit_id = uuid.UUID(ont_unit_id)

    # Update ONT with allocated IP
    ont.mgmt_ip_address = next_ip
    ont.mgmt_ip_mode = MgmtIpMode.static_ip

    db.flush()
    logger.info(
        "Allocated management IP %s from pool %s to ONT %s",
        next_ip,
        pool_id,
        ont.serial_number,
    )
    return True, f"Allocated management IP {next_ip}.", next_ip


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


def _resolve_acs_for_new_ont(db: Session, olt_id: str) -> str | None:
    """Resolve ACS server ID for a new ONT from config pack."""
    from app.services import tr069 as tr069_service

    return tr069_service.resolve_acs_server_for_ont(db, olt_id=olt_id)


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
    from app.services.network.ont_status import apply_resolved_status_for_model

    clean_serials = _serial_predicates(serial_number)
    olt = get_olt_or_none(db, olt_id)
    observed_online_status = (
        OnuOnlineStatus.online
        if str(olt_run_state or "").strip().lower() == "online"
        else None
    )

    existing = db.scalars(
        select(OntUnit).where(
            func.upper(func.replace(OntUnit.serial_number, "-", "")).in_(clean_serials),
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
            if observed_online_status is not None:
                existing.online_status = observed_online_status
                existing.offline_reason = None
                existing.last_seen_at = datetime.now(UTC)
                existing.last_sync_source = "olt_authorization"
                existing.last_sync_at = datetime.now(UTC)
            if existing.tr069_acs_server_id is None and olt is not None:
                existing.tr069_acs_server_id = olt.tr069_acs_server_id
            apply_resolved_status_for_model(existing)
            db.flush()
            return str(existing.id), f"Using existing ONT record {existing.serial_number}."
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

    display_serial = serial_number.replace("-", "")
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
        mac_address=getattr(matched_candidate, "mac", None),
        olt_device_id=uuid.UUID(str(olt_id)),
        board=board,
        port=port,
        is_active=True,
        authorization_status=OntAuthorizationStatus.authorized,
        provisioning_status=OntProvisioningStatus.unprovisioned,
        online_status=observed_online_status or OnuOnlineStatus.unknown,
        offline_reason=None,
        last_seen_at=datetime.now(UTC) if observed_online_status else None,
        last_sync_source="olt_authorization" if observed_online_status else None,
        last_sync_at=datetime.now(UTC) if observed_online_status else None,
        tr069_acs_server_id=_resolve_acs_for_new_ont(db, olt_id),
        pon_type="gpon",
        name=display_serial,
        desired_config={},
    )
    try:
        db.add(new_ont)
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
        return False, f"Failed to link assignment/PON port: {exc}"


def run_post_authorization_follow_up(
    db: Session,
    *,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int,
    skip_autofind_resolve: bool = False,
    queue_tr069_bootstrap: bool = True,
) -> tuple[bool, str, list[dict[str, object]]]:
    """Run minimal post-authorization bookkeeping.

    Args:
        db: Database session.
        ont_unit_id: UUID of the ONT unit.
        olt_id: UUID of the OLT.
        fsp: Frame/Slot/Port string.
        serial_number: ONT serial number.
        ont_id_on_olt: ONT ID on the OLT.
        skip_autofind_resolve: Skip autofind candidate resolution.
        queue_tr069_bootstrap: If True, queue a background task to bind TR-069
            profile and wait for ACS connectivity. This is non-blocking.
    """
    steps: list[dict[str, object]] = []
    assignment_ok, assignment_msg = ensure_assignment_and_pon_port_for_authorized_ont(
        db,
        ont_unit_id=ont_unit_id,
        olt_id=olt_id,
        fsp=fsp,
    )
    steps.append(
        {"name": "Link ONT to PON port", "success": assignment_ok, "message": assignment_msg}
    )
    if not assignment_ok:
        return False, assignment_msg, steps

    # Allocate management IP from OLT's pool (non-blocking failure)
    mgmt_ip_ok, mgmt_ip_msg, allocated_ip = allocate_management_ip_for_ont(
        db,
        ont_unit_id=ont_unit_id,
        olt_id=olt_id,
    )
    steps.append({
        "name": "Allocate management IP",
        "success": mgmt_ip_ok,
        "message": mgmt_ip_msg,
        "allocated_ip": allocated_ip,
    })
    if not mgmt_ip_ok:
        # Non-fatal - authorization still succeeded, but log warning
        logger.warning(
            "Failed to allocate management IP for ONT %s: %s",
            serial_number,
            mgmt_ip_msg,
        )

    # Queue background TR-069 ACS connectivity task (non-blocking)
    if queue_tr069_bootstrap:
        try:
            from app.services.queue_adapter import enqueue_task

            result = enqueue_task(
                "app.tasks.ont_authorization.ensure_tr069_acs_connectivity",
                args=[ont_unit_id, olt_id, fsp, ont_id_on_olt],
                correlation_id=f"tr069_acs_connect:{ont_unit_id}",
                source="post_authorization_follow_up",
                countdown=5,  # Small delay to let OLT sync
            )
            if result.queued:
                steps.append({
                    "name": "Queue TR-069 ACS connectivity",
                    "success": True,
                    "message": "Queued background task to bind TR-069 and wait for ACS",
                    "task_id": result.task_id,
                })
                logger.info(
                    "Queued TR-069 ACS connectivity task for ONT %s: task_id=%s",
                    serial_number,
                    result.task_id,
                )
            else:
                steps.append({
                    "name": "Queue TR-069 ACS connectivity",
                    "success": False,
                    "message": f"Failed to queue: {result.error}",
                })
                logger.warning(
                    "Failed to queue TR-069 ACS connectivity task for ONT %s: %s",
                    serial_number,
                    result.error,
                )
        except Exception as exc:
            # Non-fatal - authorization still succeeded
            steps.append({
                "name": "Queue TR-069 ACS connectivity",
                "success": False,
                "message": str(exc),
            })
            logger.warning(
                "Error queueing TR-069 ACS connectivity task for ONT %s: %s",
                serial_number,
                exc,
            )

    return True, "Authorization follow-up completed.", steps


def authorize_autofind_ont(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool = False,
    preset_id: str | None = None,
    run_post_auth_sync: bool = True,
) -> AuthorizationWorkflowResult:
    """Authorize an ONT on an OLT and persist ONT inventory state."""
    from app.services.network.olt_profile_resolution import (
        AuthorizationProfileResolution,
        ensure_ont_service_profile_match,
        resolve_authorization_profiles_from_db,
    )
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.olt_write_reconciliation import (
        verify_ont_absent,
        verify_ont_authorized,
    )

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
    ) -> AuthorizationWorkflowResult:
        return AuthorizationWorkflowResult(
            success=success,
            message=message,
            steps=steps,
            ont_unit_id=ont_unit_id,
            ont_id_on_olt=ont_id_on_olt,
            status=status,
            completed_authorization=completed_authorization,
            duration_ms=max(0, int((monotonic() - started_at) * 1000)),
        )

    olt = get_olt_or_none(db, olt_id)
    if olt is None:
        return finish(success=False, message="OLT not found", status="error")

    normalized_serial = serial_number.replace("-", "").strip().upper()
    adapter = get_protocol_adapter(olt)

    if force_reauthorize:
        force_started = monotonic()
        find_result = adapter.find_ont_by_serial(normalized_serial)
        existing = find_result.data.get("registration") if find_result.success else None
        if not find_result.success:
            add_step("Find existing ONT registration", False, find_result.message, force_started)
            return finish(success=False, message=find_result.message, status="error")
        if existing:
            delete_result = adapter.deauthorize_ont(existing.fsp, existing.onu_id)
            if not delete_result.success:
                add_step("Delete existing ONT registration", False, delete_result.message, force_started)
                return finish(success=False, message=delete_result.message, status="error")
            absence = verify_ont_absent(
                olt,
                fsp=existing.fsp,
                ont_id=existing.onu_id,
                serial_number=normalized_serial,
            )
            add_step("Delete existing ONT registration", absence.success, absence.message, force_started)
            if not absence.success:
                return finish(success=False, message=absence.message, status="error")
        else:
            add_step("Check existing ONT registration", True, "No existing registration found.", force_started)

    profile_started = monotonic()
    authorization_preset = None
    if preset_id:
        from app.models.network import AuthorizationPreset

        try:
            authorization_preset = db.get(AuthorizationPreset, uuid.UUID(str(preset_id)))
        except (TypeError, ValueError):
            authorization_preset = None

    if (
        authorization_preset is not None
        and getattr(authorization_preset, "is_active", True)
        and authorization_preset.line_profile_id is not None
        and authorization_preset.service_profile_id is not None
    ):
        authorization_profiles = AuthorizationProfileResolution(
            line_profile_id=authorization_preset.line_profile_id,
            service_profile_id=authorization_preset.service_profile_id,
            message=f"Using authorization preset '{authorization_preset.name}'.",
        )
        profiles_ok = True
        profiles_msg = authorization_profiles.message
    else:
        profiles_ok, profiles_msg, authorization_profiles = (
            resolve_authorization_profiles_from_db(db, olt)
        )
    add_step("Resolve OLT authorization profiles", profiles_ok, profiles_msg, profile_started)
    if not profiles_ok or authorization_profiles is None:
        return finish(success=False, message=profiles_msg, status="error")

    auth_started = monotonic()
    auth_result = adapter.authorize_ont(
        fsp,
        normalized_serial,
        line_profile_id=authorization_profiles.line_profile_id,
        service_profile_id=authorization_profiles.service_profile_id,
    )
    ont_id = auth_result.ont_id
    if not auth_result.success or ont_id is None:
        if _is_serial_already_registered_message(auth_result.message):
            duplicate = verify_ont_authorized(
                olt,
                fsp=fsp,
                ont_id=None,
                serial_number=normalized_serial,
            )
            if duplicate.success:
                raw_ont_id = (duplicate.details or {}).get("ont_id")
                ont_id = int(raw_ont_id) if str(raw_ont_id).isdigit() else None
                add_step(
                    "Authorize ONT on OLT",
                    True,
                    "ONT serial was already registered on the OLT; reusing registration.",
                    auth_started,
                )
            else:
                add_step("Authorize ONT on OLT", False, duplicate.message, auth_started)
                return finish(success=False, message=duplicate.message, status="error")
        else:
            message = auth_result.message or "Authorization failed"
            add_step("Authorize ONT on OLT", False, message, auth_started)
            return finish(success=False, message=message, status="error")
    else:
        add_step("Authorize ONT on OLT", True, auth_result.message, auth_started)

    verify_started = monotonic()
    verification = verify_ont_authorized(
        olt,
        fsp=fsp,
        ont_id=ont_id,
        serial_number=normalized_serial,
    )
    add_step("Verify authorization on OLT", verification.success, verification.message, verify_started)
    if not verification.success:
        return finish(
            success=False,
            message=verification.message,
            status="error",
            ont_id_on_olt=ont_id,
        )

    if ont_id is not None:
        match_started = monotonic()
        match_ok, match_msg = ensure_ont_service_profile_match(olt, fsp=fsp, ont_id=ont_id)
        add_step("Verify ONT service profile match", match_ok, match_msg, match_started)

    record_started = monotonic()
    ont_unit_id, create_msg = create_or_find_ont_for_authorized_serial(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=normalized_serial,
        ont_id_on_olt=ont_id,
        olt_run_state=str((verification.details or {}).get("run_state") or ""),
    )
    add_step("Create or find ONT record", ont_unit_id is not None, create_msg, record_started)
    if ont_unit_id is None:
        return finish(
            success=True,
            message=create_msg,
            status="warning",
            ont_id_on_olt=ont_id,
            completed_authorization=True,
        )

    resolve_started = monotonic()
    resolve_ok, resolve_msg = _resolve_authorized_autofind_candidate(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=normalized_serial,
    )
    add_step("Resolve autofind candidate", resolve_ok, resolve_msg, resolve_started)

    if run_post_auth_sync:
        follow_started = monotonic()
        follow_ok, follow_msg, _follow_steps = run_post_authorization_follow_up(
            db,
            ont_unit_id=ont_unit_id,
            olt_id=olt_id,
            fsp=fsp,
            serial_number=normalized_serial,
            ont_id_on_olt=ont_id or 0,
        )
        add_step("Post-authorization follow-up", follow_ok, follow_msg, follow_started)
        if not follow_ok:
            return finish(
                success=True,
                message=f"ONT authorization completed, but follow-up failed: {follow_msg}",
                status="warning",
                ont_unit_id=ont_unit_id,
                ont_id_on_olt=ont_id,
                completed_authorization=True,
            )

    return finish(
        success=True,
        message="ONT authorization completed.",
        status="success",
        ont_unit_id=ont_unit_id,
        ont_id_on_olt=ont_id,
        completed_authorization=True,
    )


def authorize_autofind_ont_and_provision_network_audited(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool = False,
    preset_id: str | None = None,
    request: Request | None = None,
) -> AuthorizationWorkflowResult:
    """Authorize ONT and log the action for audit trail."""
    from app.services.network.action_logging import log_network_action_result

    result = authorize_autofind_ont(
        db,
        olt_id,
        fsp,
        serial_number,
        force_reauthorize=force_reauthorize,
        preset_id=preset_id,
        run_post_auth_sync=True,
    )
    status = getattr(result, "status", "success" if result.success else "error")
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
        status_code=200 if status in {"success", "warning"} else 500,
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
    return result


class OntAuthorizationService:
    """Manages ONT authorization lifecycle on OLT devices."""

    @staticmethod
    def authorize(
        db: Session,
        ont_id: str,
        *,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
    ) -> StepResult:
        """Register an ONT serial on its assigned OLT.

        Wraps the OLT protocol adapter with DB state tracking.
        Sets ``authorization_status = authorized`` on success.

        Does NOT trigger any provisioning steps (service-ports, TR-069,
        PPPoE, etc.) — those are handled independently.

        Args:
            db: Database session.
            ont_id: OntUnit primary key.
            line_profile_id: OLT line profile ID for authorization.
            service_profile_id: OLT service profile ID for authorization.

        Returns:
            StepResult with success/failure details.
        """
        ctx, err = resolve_olt_context(db, ont_id)
        if not ctx:
            return StepResult("authorize", False, err, critical=True)

        from app.services.network.olt_profile_resolution import (
            ensure_ont_service_profile_match,
            resolve_authorization_profiles_from_db,
        )
        from app.services.network.olt_protocol_adapters import get_protocol_adapter

        if line_profile_id is None or service_profile_id is None:
            profiles_ok, profiles_msg, profiles = (
                resolve_authorization_profiles_from_db(
                    db,
                    ctx.olt,
                )
            )
            if not profiles_ok or profiles is None:
                return StepResult("authorize", False, profiles_msg, critical=True)
            line_profile_id = profiles.line_profile_id
            service_profile_id = profiles.service_profile_id

        auth_result = get_protocol_adapter(ctx.olt).authorize_ont(
            ctx.fsp,
            ctx.ont.serial_number,
            line_profile_id=line_profile_id,
            service_profile_id=service_profile_id,
        )
        ok = auth_result.success
        msg = auth_result.message
        olt_ont_id = auth_result.ont_id

        if ok:
            from app.services.network.olt_write_reconciliation import (
                verify_ont_authorized,
            )

            set_authorization_status(
                ctx.ont, OntAuthorizationStatus.pending, strict=False
            )
            if olt_ont_id is not None:
                ctx.ont.external_id = str(olt_ont_id)

            verification = verify_ont_authorized(
                ctx.olt,
                fsp=ctx.fsp,
                ont_id=olt_ont_id,
                serial_number=ctx.ont.serial_number,
            )
            if verification.success:
                if olt_ont_id is not None:
                    match_ok, match_msg = ensure_ont_service_profile_match(
                        ctx.olt,
                        fsp=ctx.fsp,
                        ont_id=olt_ont_id,
                    )
                    if not match_ok:
                        set_provisioning_status(
                            ctx.ont,
                            OntProvisioningStatus.drift_detected,
                            strict=False,
                        )
                        db.flush()
                        return StepResult("authorize", False, match_msg, critical=True)
                set_authorization_status(
                    ctx.ont, OntAuthorizationStatus.authorized, strict=False
                )
                db.flush()
                logger.info(
                    "ONT %s authorized on OLT %s (ONT-ID %s)",
                    ctx.ont.serial_number,
                    ctx.olt.name,
                    olt_ont_id,
                )
            else:
                set_provisioning_status(
                    ctx.ont,
                    OntProvisioningStatus.drift_detected,
                    strict=False,
                )
                db.flush()
                logger.warning(
                    "ONT %s authorization write accepted on OLT %s but verification failed: %s",
                    ctx.ont.serial_number,
                    ctx.olt.name,
                    verification.message,
                )
                return StepResult(
                    "authorize", False, verification.message, critical=True
                )
        else:
            logger.warning(
                "ONT %s authorization failed on OLT %s: %s",
                ctx.ont.serial_number,
                ctx.olt.name,
                msg,
            )

        return StepResult("authorize", ok, msg, critical=True)

    @staticmethod
    def deauthorize(db: Session, ont_id: str) -> StepResult:
        """Remove an ONT registration from its OLT.

        Wraps the OLT protocol adapter with DB state tracking.
        Sets ``authorization_status = unauthorized`` on success.

        Args:
            db: Database session.
            ont_id: OntUnit primary key.

        Returns:
            StepResult with success/failure details.
        """
        from app.services.network.olt_protocol_adapters import get_protocol_adapter

        ctx, err = resolve_olt_context(db, ont_id)
        if not ctx:
            return StepResult("deauthorize", False, err, critical=True)

        deauth_result = get_protocol_adapter(ctx.olt).deauthorize_ont(
            ctx.fsp,
            ctx.olt_ont_id,
        )
        ok = deauth_result.success
        msg = deauth_result.message

        if ok:
            from app.services.network.olt_write_reconciliation import (
                verify_ont_absent,
            )

            set_authorization_status(
                ctx.ont, OntAuthorizationStatus.pending, strict=False
            )
            verification = verify_ont_absent(
                ctx.olt,
                fsp=ctx.fsp,
                ont_id=ctx.olt_ont_id,
                serial_number=ctx.ont.serial_number,
            )
            if verification.success:
                set_authorization_status(
                    ctx.ont, OntAuthorizationStatus.deauthorized, strict=False
                )
                db.flush()
                logger.info(
                    "ONT %s deauthorized from OLT %s",
                    ctx.ont.serial_number,
                    ctx.olt.name,
                )
            else:
                set_provisioning_status(
                    ctx.ont,
                    OntProvisioningStatus.drift_detected,
                    strict=False,
                )
                db.flush()
                logger.warning(
                    "ONT %s deauthorization write accepted on OLT %s but verification failed: %s",
                    ctx.ont.serial_number,
                    ctx.olt.name,
                    verification.message,
                )
                return StepResult(
                    "deauthorize", False, verification.message, critical=True
                )
        else:
            logger.warning(
                "ONT %s deauthorization failed on OLT %s: %s",
                ctx.ont.serial_number,
                ctx.olt.name,
                msg,
            )

        return StepResult("deauthorize", ok, msg, critical=True)

    @staticmethod
    def check_status(db: Session, ont_id: str) -> StepResult:
        """Query the OLT to verify the ONT's current authorization state.

        Returns:
            StepResult with the current authorization state in the message.
        """
        ont = db.get(OntUnit, ont_id)
        if not ont:
            return StepResult("check_status", False, "ONT not found", critical=False)

        current = ont.authorization_status
        status_str = current.value if current else "unknown"
        return StepResult(
            "check_status",
            True,
            f"Current authorization status: {status_str}",
            critical=False,
        )


# Singleton
ont_authorization = OntAuthorizationService()
