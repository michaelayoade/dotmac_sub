"""ONT authorization service - OLT serial registration with DB state tracking.

Authorization runs synchronously because the OLT work is OMCI/CLI-driven. The
workflow registers the autofind serial, persists local inventory state, links the
PON assignment, allocates management IP, and applies the OLT-side ACS foundation
before returning.
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
    MgmtIpMode,
    OLTDevice,
    OntAssignment,
    OntAuthorizationStatus,
    OntProvisioningStatus,
    OntUnit,
    OnuOnlineStatus,
)
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.ont_provisioning.context import resolve_olt_context
from app.services.network.ont_provisioning.result import StepResult
from app.services.network.ont_status import (
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
    partial_success: bool = False
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

    from app.models.network import IpBlock, IpPool, IPv4Address, OntAssignment

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

    # Get IPs actively held in ipv4_addresses. Released inventory rows can
    # remain for audit/history, but should not consume pool capacity.
    address_rows = db.scalars(
        select(IPv4Address).where(IPv4Address.pool_id == pool.id)
    ).all()
    used = {
        str(address.address)
        for address in address_rows
        if address.is_reserved
        or address.ont_unit_id is not None
        or getattr(address, "assignment", None) is not None
    }

    # Also get IPs assigned to ONTs (source of truth for management IPs)
    # These may not be tracked in ipv4_addresses yet
    assigned_ips = db.scalars(
        select(OntAssignment.mgmt_ip_address).where(
            OntAssignment.mgmt_ip_address.isnot(None),
            OntAssignment.active.is_(True),
        )
    ).all()
    for ip in assigned_ips:
        if ip:
            used.add(str(ip))

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


def _pool_contains_ipv4_address(
    db: Session,
    *,
    pool_id: str | uuid.UUID,
    address: str,
) -> bool:
    """Return whether address belongs to an active IPv4 block in the pool."""
    import ipaddress

    from app.models.network import IpBlock

    try:
        ip_address = ipaddress.ip_address(str(address))
    except ValueError:
        return False
    if ip_address.version != 4:
        return False

    blocks = db.scalars(
        select(IpBlock)
        .where(IpBlock.pool_id == pool_id)
        .where(IpBlock.is_active.is_(True))
    ).all()
    for block in blocks:
        try:
            network = ipaddress.ip_network(str(block.cidr), strict=False)
        except ValueError:
            continue
        if network.version == 4 and ip_address in network:
            return True
    return False


def _management_ip_is_available(
    db: Session,
    *,
    pool_id: str | uuid.UUID,
    address: str | None,
) -> bool:
    """Return whether an IPv4 address is currently free for management allocation."""
    from app.models.network import IPv4Address, OntAssignment

    candidate = str(address or "").strip()
    if not candidate:
        return False
    if not _pool_contains_ipv4_address(db, pool_id=pool_id, address=candidate):
        return False

    record = db.scalars(
        select(IPv4Address).where(IPv4Address.address == candidate)
    ).first()
    if record is not None and record.pool_id is not None and str(record.pool_id) != str(pool_id):
        return False
    if record is not None and (
        record.is_reserved
        or record.ont_unit_id is not None
        or getattr(record, "assignment", None) is not None
    ):
        return False

    assigned = db.scalar(
        select(OntAssignment.id)
        .where(OntAssignment.mgmt_ip_address == candidate)
        .where(OntAssignment.active.is_(True))
        .limit(1)
    )
    return assigned is None


def _advance_management_pool_cache_after_allocation(
    db: Session,
    *,
    pool,
    allocated_ip: str,
) -> None:
    """Advance cached next IP without recomputing full pool availability."""
    import ipaddress

    from app.models.network import IpBlock

    try:
        allocated = ipaddress.ip_address(str(allocated_ip))
    except ValueError:
        pool.next_available_ip = None
        pool.available_count = None
        return

    next_available: str | None = None
    blocks = db.scalars(
        select(IpBlock)
        .where(IpBlock.pool_id == pool.id)
        .where(IpBlock.is_active.is_(True))
        .order_by(IpBlock.cidr.asc())
    ).all()
    for block in blocks:
        try:
            network = ipaddress.ip_network(str(block.cidr), strict=False)
        except ValueError:
            continue
        if network.version != 4 or allocated not in network:
            continue
        for ip_address in network.hosts():
            if ip_address <= allocated:
                continue
            candidate = str(ip_address)
            if _management_ip_is_available(
                db,
                pool_id=pool.id,
                address=candidate,
            ):
                next_available = candidate
                break
        if next_available is not None:
            break

    pool.next_available_ip = next_available
    if pool.available_count is not None and pool.available_count > 0:
        pool.available_count -= 1


def allocate_management_ip_for_ont(
    db: Session,
    *,
    ont_unit_id: str,
    olt_id: str,
) -> tuple[bool, str, str | None]:
    """Allocate a management IP from the OLT's management IP pool for the ONT.

    The IP is stored on the ONT's active assignment (ont_assignments.mgmt_ip_address),
    which is the source of truth read by resolve_effective_ont_config().

    Returns:
        Tuple of (success, message, allocated_ip).
    """
    from app.models.network import IPv4Address

    ont = db.get(OntUnit, ont_unit_id)
    if ont is None:
        return False, "ONT not found.", None

    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return False, "OLT not found.", None

    # Get or create the active assignment for this ONT
    assignment = _get_or_create_active_assignment(db, ont)

    # Get management IP pool from OLT config pack
    pool_id = olt.mgmt_ip_pool_id
    if not pool_id:
        logger.warning(
            "No management IP pool configured for OLT %s; cannot authorize ONT %s onto ACS",
            olt.name,
            ont.serial_number,
        )
        return False, "No management IP pool configured on OLT.", None

    # Serialize allocation for this pool. refresh_pool_availability() recomputes
    # from current reservations, so concurrent authorizations must not pass that
    # read before either transaction has reserved its selected address.
    from app.models.network import IpPool

    locked_pool = db.scalars(
        select(IpPool).where(IpPool.id == pool_id).with_for_update()
    ).first()
    if locked_pool is None:
        return False, "Management IP pool not found.", None

    # Reuse an existing management IP only if it is valid for this OLT pool.
    if assignment.mgmt_ip_address:
        existing_ip = str(assignment.mgmt_ip_address).strip()
        record = db.scalars(
            select(IPv4Address).where(IPv4Address.address == existing_ip)
        ).first()
        record_pool_id = getattr(record, "pool_id", None)
        record_ont_id = getattr(record, "ont_unit_id", None)
        owned_by_other_ont = record_ont_id is not None and str(record_ont_id) != str(ont.id)
        assigned_elsewhere = getattr(record, "assignment", None) is not None
        tied_to_other_pool = record_pool_id is not None and str(record_pool_id) != str(pool_id)
        reserved_for_other = bool(getattr(record, "is_reserved", False)) and str(
            getattr(record, "notes", "") or ""
        ) not in {"", f"ont:{ont_unit_id}", f"Reserved for ONT {ont_unit_id}"}
        in_current_pool = _pool_contains_ipv4_address(
            db,
            pool_id=pool_id,
            address=existing_ip,
        )
        if (
            in_current_pool
            and not tied_to_other_pool
            and not owned_by_other_ont
            and not assigned_elsewhere
            and not reserved_for_other
        ):
            if record is None:
                record = IPv4Address(
                    address=existing_ip,
                    pool_id=pool_id,
                    is_reserved=True,
                    notes=f"ont:{ont_unit_id}",
                )
                db.add(record)
            else:
                record.pool_id = pool_id
                record.is_reserved = True
                record.notes = f"ont:{ont_unit_id}"
            record.ont_unit_id = ont.id
            record.allocation_type = "management"
            db.flush()
            return (
                True,
                f"ONT already has management IP {existing_ip}.",
                existing_ip,
            )

        logger.warning(
            "Clearing stale management IP %s for ONT %s before allocating from OLT pool %s",
            existing_ip,
            ont.serial_number,
            pool_id,
        )
        if record is not None and str(getattr(record, "ont_unit_id", "")) == str(ont.id):
            record.is_reserved = False
            record.notes = None
            record.ont_unit_id = None
            record.allocation_type = None
        assignment.mgmt_ip_address = None
        assignment.mgmt_ip_mode = MgmtIpMode.inactive
        assignment.mgmt_subnet = None
        assignment.mgmt_gateway = None
        db.flush()

    next_ip = str(getattr(locked_pool, "next_available_ip", "") or "").strip() or None
    if next_ip and not _management_ip_is_available(
        db,
        pool_id=pool_id,
        address=next_ip,
    ):
        next_ip = None

    if next_ip is None:
        next_ip, _available_count = refresh_pool_availability(db, pool_id)
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
    record = db.scalars(
        select(IPv4Address).where(IPv4Address.address == next_ip)
    ).first()
    if record is None:
        record = IPv4Address(
            address=next_ip,
            pool_id=pool_id,
            is_reserved=True,
            notes=note,
        )
        db.add(record)
    else:
        if record.pool_id and str(record.pool_id) != str(pool_id):
            return (
                False,
                f"Management IP {next_ip} belongs to a different pool.",
                None,
            )
        record.pool_id = pool_id
        record.is_reserved = True
        record.notes = note
    record.ont_unit_id = ont.id
    record.allocation_type = "management"

    # Update assignment with allocated IP (source of truth for effective config)
    assignment.mgmt_ip_address = next_ip
    assignment.mgmt_ip_mode = MgmtIpMode.static_ip
    _advance_management_pool_cache_after_allocation(
        db,
        pool=locked_pool,
        allocated_ip=next_ip,
    )

    db.flush()
    logger.info(
        "Allocated management IP %s from pool %s to ONT %s",
        next_ip,
        pool_id,
        ont.serial_number,
    )
    return True, f"Allocated management IP {next_ip}.", next_ip


def _get_or_create_active_assignment(db: Session, ont: OntUnit) -> OntAssignment:
    """Get the active assignment for an ONT, creating one if none exists."""
    from app.services import web_network_ont_assignments as assignments_service

    return assignments_service.get_or_create_active_assignment(db, ont)


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
    observed_olt_status = (
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
            if observed_olt_status is not None:
                existing.olt_status = observed_olt_status
                existing.offline_reason = None
                existing.last_seen_at = datetime.now(UTC)
                existing.last_sync_source = "olt_authorization"
                existing.last_sync_at = datetime.now(UTC)
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


def apply_authorization_foundation(
    db: Session,
    *,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int,
) -> tuple[bool, str, list[dict[str, object]]]:
    """Run synchronous ACS foundation setup during ONT authorization.

    Args:
        db: Database session.
        ont_unit_id: UUID of the ONT unit.
        olt_id: UUID of the OLT.
        fsp: Frame/Slot/Port string.
        serial_number: ONT serial number.
        ont_id_on_olt: ONT ID on the OLT.
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

    # Allocate management IP from the OLT pool before applying the ACS
    # foundation. Without this, authorization can finish while the ONT has no
    # management path for TR-069 reachability.
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
        logger.warning(
            "Failed to allocate management IP for ONT %s: %s",
            serial_number,
            mgmt_ip_msg,
        )
        return False, mgmt_ip_msg, steps

    try:
        from app.services.network.acs_foundation import apply_acs_foundation

        foundation_result = apply_acs_foundation(
            db,
            ont_unit_id=ont_unit_id,
            olt_id=olt_id,
            fsp=fsp,
            ont_id_on_olt=ont_id_on_olt,
        )
        steps.append({
            "name": "Apply ACS foundation",
            "success": bool(foundation_result.get("success")),
            "message": str(foundation_result.get("message") or ""),
            "data": foundation_result,
        })
    except Exception as exc:
        foundation_step = {
            "name": "Apply ACS foundation",
            "success": False,
            "message": str(exc),
        }
        steps.append(foundation_step)
        logger.warning(
            "Error applying ACS foundation for ONT %s: %s",
            serial_number,
            exc,
        )
    if not bool(steps[-1].get("success")):
        message = "Authorization foundation failed: ACS foundation was not applied."
        return False, message, steps

    return True, "Authorization foundation completed.", steps


def verify_authorization_acs_readiness(
    db: Session,
    *,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int,
) -> tuple[bool, str, list[dict[str, object]]]:
    """Synchronously verify OLT readback, management reachability, and ACS inform."""
    from app.services import ping as ping_service
    from app.services.network.effective_ont_config import resolve_effective_ont_config
    from app.services.network.olt_write_reconciliation import verify_ont_authorized
    from app.services.network.ont_provision_steps import wait_tr069_bootstrap

    steps: list[dict[str, object]] = []

    olt = db.get(OLTDevice, olt_id)
    ont = db.get(OntUnit, ont_unit_id)
    if olt is None:
        return False, "OLT not found for ACS readiness verification.", [
            {
                "name": "Verify OLT authorization readback",
                "success": False,
                "message": "OLT not found.",
            }
        ]
    if ont is None:
        return False, "ONT not found for ACS readiness verification.", [
            {
                "name": "Verify OLT authorization readback",
                "success": False,
                "message": "ONT not found.",
            }
        ]

    readback = verify_ont_authorized(
        olt,
        fsp=fsp,
        ont_id=ont_id_on_olt,
        serial_number=serial_number,
    )
    steps.append(
        {
            "name": "Verify OLT authorization readback",
            "success": readback.success,
            "message": readback.message,
            "data": readback.details,
        }
    )
    if not readback.success:
        return False, readback.message, steps

    effective = resolve_effective_ont_config(db, ont)
    values = effective.get("values", {})
    mgmt_ip = values.get("mgmt_ip_address")
    if mgmt_ip:
        ping_ok, latency_ms = ping_service.run_ping(str(mgmt_ip), timeout_seconds=4)
        ping_message = (
            f"Management IP {mgmt_ip} is reachable"
            + (f" ({latency_ms:.1f} ms)." if latency_ms is not None else ".")
            if ping_ok
            else f"Management IP {mgmt_ip} is not reachable from the app/ACS network."
        )
        steps.append(
            {
                "name": "Verify management IP reachability",
                "success": ping_ok,
                "message": ping_message,
                "mgmt_ip": str(mgmt_ip),
                "latency_ms": latency_ms,
            }
        )
        if not ping_ok:
            return False, ping_message, steps
    else:
        steps.append(
            {
                "name": "Verify management IP reachability",
                "success": True,
                "message": "No static management IP configured; skipping ping reachability check.",
                "skipped": True,
            }
        )

    acs_configured = bool(
        values.get("tr069_acs_server_id")
        or values.get("tr069_olt_profile_id")
        or getattr(olt, "tr069_acs_server_id", None)
    )
    if not acs_configured:
        steps.append(
            {
                "name": "Wait for ACS inform",
                "success": True,
                "message": "No ACS/TR-069 configuration resolved; skipping ACS inform wait.",
                "skipped": True,
            }
        )
        return True, "ACS readiness verification skipped because ACS is not configured.", steps

    wait_result = wait_tr069_bootstrap(db, ont_unit_id, allow_blocking=True)
    steps.append(
        {
            "name": "Wait for ACS inform",
            "success": wait_result.success,
            "message": wait_result.message,
            "duration_ms": wait_result.duration_ms,
        }
    )
    if not wait_result.success:
        return False, wait_result.message, steps

    return True, "ONT is authorized, reachable, and observed in ACS.", steps


def _first_failed_step(steps: list[dict[str, object]]) -> dict[str, object] | None:
    return next((step for step in steps if not bool(step.get("success"))), None)


def _step_message(default: str, steps: list[dict[str, object]]) -> str:
    failed = _first_failed_step(steps)
    if failed is not None:
        return str(failed.get("message") or default)
    if steps:
        return "; ".join(
            str(step.get("message") or "")
            for step in steps
            if str(step.get("message") or "")
        ) or default
    return default


def _append_grouped_step(
    result: AuthorizationWorkflowResult,
    *,
    name: str,
    success: bool,
    message: str,
    duration_ms: int = 0,
) -> None:
    result.steps.append(
        AuthorizationStepResult(
            step=len(result.steps) + 1,
            name=name,
            success=success,
            message=message,
            duration_ms=duration_ms,
        )
    )


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
        resolve_authorization_profiles_from_db,
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

    from app.services.network.olt_config_pack import (
        get_validation_summary,
        validate_config_pack_comprehensive,
    )

    validation_started = monotonic()
    config_validation = validate_config_pack_comprehensive(db, olt_id)
    validation_message = get_validation_summary(config_validation)
    add_step(
        "Validate OLT config pack",
        config_validation.is_valid,
        validation_message,
        validation_started,
    )
    if not config_validation.is_valid:
        return finish(
            success=False,
            message=validation_message,
            status="error",
        )

    normalized_serial = serial_number.replace("-", "").strip().upper()
    adapter = get_protocol_adapter(olt)

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
                return finish(success=False, message=delete_result.message, status="error")
            absence = verify_ont_absent(
                olt,
                fsp=existing.fsp,
                ont_id=existing.onu_id,
                serial_number=normalized_serial,
            )
            if not absence.success:
                add_step("Activate ONT", False, absence.message, force_started)
                return finish(success=False, message=absence.message, status="error")

    activation_started = monotonic()
    authorization_preset = None
    authorization_profiles: AuthorizationProfileResolution | None = None
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
    if not profiles_ok or authorization_profiles is None:
        add_step(
            "Activate ONT",
            False,
            profiles_msg,
            activation_started,
        )
        return finish(success=False, message=profiles_msg, status="error")

    auth_result = adapter.authorize_ont(
        fsp,
        normalized_serial,
        line_profile_id=authorization_profiles.line_profile_id,
        service_profile_id=authorization_profiles.service_profile_id,
    )
    ont_id = auth_result.ont_id
    if not auth_result.success or ont_id is None:
        if _is_serial_already_registered_message(auth_result.message):
            find_result = adapter.find_ont_by_serial(normalized_serial)
            existing = find_result.data.get("registration") if find_result.success else None
            if existing is not None and str(getattr(existing, "fsp", "")).strip() == fsp:
                raw_ont_id = getattr(existing, "onu_id", None)
                ont_id = int(raw_ont_id) if raw_ont_id is not None else None
                add_step(
                    "Activate ONT",
                    True,
                    "ONT serial was already registered on the OLT; reusing registration.",
                    activation_started,
                )
            else:
                if not find_result.success:
                    add_step("Activate ONT", False, find_result.message, activation_started)
                    return finish(success=False, message=find_result.message, status="error")
                if existing is None:
                    message = "ONT serial already exists, but the existing registration could not be found."
                    add_step("Activate ONT", False, message, activation_started)
                    return finish(success=False, message=message, status="error")

                delete_result = adapter.deauthorize_ont(existing.fsp, existing.onu_id)
                if not delete_result.success:
                    add_step("Activate ONT", False, delete_result.message, activation_started)
                    return finish(
                        success=False,
                        message=delete_result.message,
                        status="error",
                    )
                absence = verify_ont_absent(
                    olt,
                    fsp=existing.fsp,
                    ont_id=existing.onu_id,
                    serial_number=normalized_serial,
                )
                if not absence.success:
                    add_step("Activate ONT", False, absence.message, activation_started)
                    return finish(success=False, message=absence.message, status="error")

                auth_result = adapter.authorize_ont(
                    fsp,
                    normalized_serial,
                    line_profile_id=authorization_profiles.line_profile_id,
                    service_profile_id=authorization_profiles.service_profile_id,
                )
                ont_id = auth_result.ont_id
                if not auth_result.success or ont_id is None:
                    message = (
                        "Removed existing ONT registration, but authorization on the "
                        f"requested port failed: {auth_result.message or 'Authorization failed'}"
                    )
                    add_step("Activate ONT", False, message, activation_started)
                    return finish(success=False, message=message, status="error")
                auth_result.message = (
                    "Removed existing ONT registration from "
                    f"{existing.fsp} and authorized on {fsp}. {auth_result.message}"
                ).strip()
        else:
            message = auth_result.message or "Authorization failed"
            add_step("Activate ONT", False, message, activation_started)
            return finish(success=False, message=message, status="error")

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

    resolve_ok, resolve_msg = _resolve_authorized_autofind_candidate(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=normalized_serial,
    )
    state_msg = create_msg if resolve_ok else f"{create_msg} {resolve_msg}".strip()
    activation_message = (
        f"{profiles_msg} {auth_result.message} {state_msg}".strip()
        if auth_result.success
        else state_msg
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
    """Authorize ONT and log the action for audit trail.

    Commits on success.
    """
    from app.services.network.action_logging import log_network_action_result

    started_at = monotonic()
    result = authorize_autofind_ont(
        db,
        olt_id,
        fsp,
        serial_number,
        force_reauthorize=force_reauthorize,
        preset_id=preset_id,
    )

    if result.success:
        if result.ont_unit_id and result.ont_id_on_olt is not None:
            try:
                foundation_ok, foundation_msg, foundation_steps = (
                    apply_authorization_foundation(
                        db,
                        ont_unit_id=result.ont_unit_id,
                        olt_id=olt_id,
                        fsp=fsp,
                        serial_number=serial_number,
                        ont_id_on_olt=result.ont_id_on_olt,
                    )
                )
                foundation_message = _step_message(foundation_msg, foundation_steps)
                if not foundation_ok:
                    _append_grouped_step(
                        result,
                        name="Bring ONT onto ACS",
                        success=False,
                        message=foundation_message,
                    )
                    result.success = False
                    result.partial_success = True
                    result.status = "error"
                    result.message = (
                        "ONT authorized, but ACS foundation setup failed: "
                        f"{foundation_msg}"
                    )
                else:
                    logger.info(
                        "Applied ACS foundation for ONT %s: %s (%d steps)",
                        serial_number,
                        foundation_msg,
                        len(foundation_steps),
                    )
                    readiness_ok, readiness_msg, readiness_steps = (
                        verify_authorization_acs_readiness(
                            db,
                            ont_unit_id=result.ont_unit_id,
                            olt_id=olt_id,
                            fsp=fsp,
                            serial_number=serial_number,
                            ont_id_on_olt=result.ont_id_on_olt,
                        )
                    )
                    readiness_message = _step_message(readiness_msg, readiness_steps)
                    readiness_duration_ms = sum(
                        int(step.get("duration_ms") or 0)
                        for step in readiness_steps
                    )
                    acs_message = (
                        readiness_message
                        if readiness_ok
                        else f"{foundation_message} {readiness_message}".strip()
                    )
                    _append_grouped_step(
                        result,
                        name="Bring ONT onto ACS",
                        success=readiness_ok,
                        message=acs_message,
                        duration_ms=readiness_duration_ms,
                    )
                    if not readiness_ok:
                        result.success = False
                        result.partial_success = True
                        result.status = "error"
                        result.message = (
                            "ONT authorized and ACS foundation applied, but "
                            f"ACS readiness verification failed: {readiness_msg}"
                        )
                    else:
                        result.message = readiness_msg
            except Exception as exc:
                db.rollback()
                logger.warning(
                    "Authorization succeeded but ACS foundation setup failed for ONT %s: %s",
                    serial_number,
                    exc,
                )
                result.steps.append(
                    AuthorizationStepResult(
                        step=len(result.steps) + 1,
                        name="Bring ONT onto ACS",
                        success=False,
                        message=str(exc),
                    )
                )
                result.success = False
                result.partial_success = True
                result.status = "error"
                result.message = "ONT authorized, but ACS foundation setup failed."
    if not result.success:
        db.rollback()
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
    result.duration_ms = max(0, int((monotonic() - started_at) * 1000))
    db.commit()
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
            set_authorization_status(
                ctx.ont, OntAuthorizationStatus.authorized, strict=False
            )
            if olt_ont_id is not None:
                ctx.ont.external_id = str(olt_ont_id)
            db.flush()
            logger.info(
                "ONT %s authorized on OLT %s (ONT-ID %s)",
                ctx.ont.serial_number,
                ctx.olt.name,
                olt_ont_id,
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
                    OntProvisioningStatus.failed,
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
