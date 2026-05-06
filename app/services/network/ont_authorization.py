"""ONT authorization service - OLT serial registration with DB state tracking.

Authorization runs synchronously because the OLT work is OMCI/CLI-driven. The
workflow registers the autofind serial, persists local inventory state, links the
PON assignment, allocates management IP, and applies the OLT-side ACS foundation
before returning.
"""

from __future__ import annotations

import logging
import ipaddress
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING

from sqlalchemy import select
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
from app.services.network.ont_desired_config import (
    desired_config_column,
    get_desired_config_value,
    set_desired_config_values,
)
from app.services.network.equipment_identity import normalize_ont_equipment_id
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


def _subnet_mask_from_cidr(cidr: str | None) -> str | None:
    if not cidr:
        return None
    try:
        return str(ipaddress.ip_network(str(cidr), strict=False).netmask)
    except ValueError:
        return None


def _management_pool_subnet(pool: object) -> str | None:
    return _subnet_mask_from_cidr(getattr(pool, "cidr", None))


def _management_pool_gateway(pool: object) -> str | None:
    gateway = str(getattr(pool, "gateway", "") or "").strip()
    return gateway or None


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


# ---------------------------------------------------------------------------
# Management IP allocation
# ---------------------------------------------------------------------------


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
    if (
        record is not None
        and record.pool_id is not None
        and str(record.pool_id) != str(pool_id)
    ):
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
    if assigned is not None:
        return False
    return candidate not in _desired_management_ips(db)


def _desired_management_ips(db: Session) -> set[str]:
    """Return management IPs stored in ONT desired_config."""
    rows = db.scalars(select(desired_config_column(OntUnit))).all()
    ips: set[str] = set()
    for config in rows:
        if not isinstance(config, dict):
            continue
        value = get_desired_config_value(config, "management", "ip_address")
        if value:
            ips.add(str(value))
    return ips


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

    assigned_ips = db.scalars(
        select(OntAssignment.mgmt_ip_address).where(
            OntAssignment.mgmt_ip_address.isnot(None),
            OntAssignment.active.is_(True),
        )
    ).all()
    for ip in assigned_ips:
        if ip:
            used.add(str(ip))
    used.update(_desired_management_ips(db))

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


def _advance_pool_cache_after_allocation(
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
    if not isinstance(allocated, ipaddress.IPv4Address):
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
        if not isinstance(network, ipaddress.IPv4Network) or allocated not in network:
            continue
        for ip_address in network.hosts():
            if ip_address <= allocated:
                continue
            candidate = str(ip_address)
            if _management_ip_is_available(db, pool_id=pool.id, address=candidate):
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

    The IP is stored on OntUnit.desired_config. Legacy assignment columns are
    updated during the transition for compatibility with old pool scans.

    Returns:
        Tuple of (success, message, allocated_ip).
        If no pool is configured, returns a failure because ACS reachability cannot
        be established without a management address.
    """
    from app.models.network import IpPool, IPv4Address
    from app.services.network.ont_management_ipam import allocate_ont_management_ip

    ont = db.get(OntUnit, ont_unit_id)
    if ont is None:
        return False, "ONT not found.", None

    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return False, "OLT not found.", None

    try:
        allocation = allocate_ont_management_ip(db, ont=ont, olt=olt)
    except ValueError as exc:
        return False, str(exc), None
    return (
        True,
        (
            f"ONT already has management IP {allocation.address}."
            if allocation.reused
            else f"Allocated management IP {allocation.address}."
        ),
        allocation.address,
    )

    assignment = _get_or_create_active_assignment(db, ont)

    pool_id = olt.mgmt_ip_pool_id
    if not pool_id:
        return False, "No management IP pool configured on OLT.", None

    # Serialize allocation for this pool
    locked_pool = db.scalars(
        select(IpPool).where(IpPool.id == pool_id).with_for_update()
    ).first()
    if locked_pool is None:
        return False, "Management IP pool not found.", None

    # Reuse existing management IP if valid for this pool
    if assignment.mgmt_ip_address:
        existing_ip = str(assignment.mgmt_ip_address).strip()
        record = db.scalars(
            select(IPv4Address).where(IPv4Address.address == existing_ip)
        ).first()
        record_ont_id = getattr(record, "ont_unit_id", None)
        owned_by_other_ont = record_ont_id is not None and str(record_ont_id) != str(
            ont.id
        )
        in_current_pool = _pool_contains_ipv4_address(
            db, pool_id=pool_id, address=existing_ip
        )

        if in_current_pool and not owned_by_other_ont:
            mgmt_subnet = _management_pool_subnet(locked_pool)
            mgmt_gateway = _management_pool_gateway(locked_pool)
            if not mgmt_subnet:
                return False, "Management IP pool CIDR is invalid or missing.", None
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
            assignment.mgmt_ip_mode = MgmtIpMode.static_ip
            assignment.mgmt_subnet = mgmt_subnet
            assignment.mgmt_gateway = mgmt_gateway
            set_desired_config_values(
                ont,
                {
                    "management.ip_address": existing_ip,
                    "management.ip_mode": "static_ip",
                    "management.subnet": mgmt_subnet,
                    "management.gateway": mgmt_gateway,
                },
            )
            db.flush()
            return True, f"ONT already has management IP {existing_ip}.", existing_ip

        # Clear stale IP
        if record is not None and str(getattr(record, "ont_unit_id", "")) == str(
            ont.id
        ):
            record.is_reserved = False
            record.notes = None
            record.ont_unit_id = None
            record.allocation_type = None
        assignment.mgmt_ip_address = None
        assignment.mgmt_subnet = None
        assignment.mgmt_gateway = None
        assignment.mgmt_ip_mode = MgmtIpMode.inactive
        set_desired_config_values(
            ont,
            {
                "management.ip_address": None,
                "management.ip_mode": "inactive",
                "management.subnet": None,
                "management.gateway": None,
            },
        )
        db.flush()

    # Find next available IP
    next_ip = str(getattr(locked_pool, "next_available_ip", "") or "").strip() or None
    if next_ip and not _management_ip_is_available(
        db, pool_id=pool_id, address=next_ip
    ):
        next_ip = None

    if next_ip is None:
        next_ip, _ = refresh_pool_availability(db, pool_id)
    if not next_ip:
        return False, "Management IP pool exhausted.", None

    # Reserve the IP
    record = db.scalars(
        select(IPv4Address).where(IPv4Address.address == next_ip)
    ).first()
    if record is None:
        record = IPv4Address(
            address=next_ip,
            pool_id=pool_id,
            is_reserved=True,
            notes=f"ont:{ont_unit_id}",
        )
        db.add(record)
    else:
        if record.pool_id and str(record.pool_id) != str(pool_id):
            return False, f"Management IP {next_ip} belongs to a different pool.", None
        record.pool_id = pool_id
        record.is_reserved = True
        record.notes = f"ont:{ont_unit_id}"
    record.ont_unit_id = ont.id
    record.allocation_type = "management"

    mgmt_subnet = _management_pool_subnet(locked_pool)
    mgmt_gateway = _management_pool_gateway(locked_pool)
    if not mgmt_subnet:
        return False, "Management IP pool CIDR is invalid or missing.", None
    assignment.mgmt_ip_address = next_ip
    assignment.mgmt_ip_mode = MgmtIpMode.static_ip
    assignment.mgmt_subnet = mgmt_subnet
    assignment.mgmt_gateway = mgmt_gateway
    set_desired_config_values(
        ont,
        {
            "management.ip_address": next_ip,
            "management.ip_mode": "static_ip",
            "management.subnet": mgmt_subnet,
            "management.gateway": mgmt_gateway,
        },
    )
    _advance_pool_cache_after_allocation(db, pool=locked_pool, allocated_ip=next_ip)

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


def apply_authorization_foundation(
    db: Session,
    *,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int,
) -> tuple[bool, str, list[dict[str, object]]]:
    """Run post-authorization setup: link PON port, allocate IP, apply ACS foundation.

    Returns:
        Tuple of (success, message, steps).
    """
    from app.services.network.acs_foundation import apply_acs_foundation

    steps: list[dict[str, object]] = []

    # Link PON port
    port_ok, port_msg = ensure_assignment_and_pon_port_for_authorized_ont(
        db,
        ont_unit_id=ont_unit_id,
        olt_id=olt_id,
        fsp=fsp,
    )
    steps.append(
        {"name": "Link ONT to PON port", "success": port_ok, "message": port_msg}
    )
    if not port_ok:
        return False, port_msg, steps

    # Allocate management IP
    ip_ok, ip_msg, allocated_ip = allocate_management_ip_for_ont(
        db,
        ont_unit_id=ont_unit_id,
        olt_id=olt_id,
    )
    steps.append(
        {
            "name": "Allocate management IP",
            "success": ip_ok,
            "message": ip_msg,
            "allocated_ip": allocated_ip,
        }
    )
    if not ip_ok:
        return False, ip_msg, steps

    ont = db.get(OntUnit, ont_unit_id)
    olt = db.get(OLTDevice, olt_id)
    acs_prereq_step: dict[str, object] = {
        "name": "Verify ACS prerequisites",
        "success": True,
        "message": "ACS prerequisites resolved.",
    }
    if not ont:
        acs_prereq_step.update({"success": False, "message": "ONT not found."})
        steps.append(acs_prereq_step)
        return False, "ONT not found.", steps
    if not olt:
        acs_prereq_step.update({"success": False, "message": "OLT not found."})
        steps.append(acs_prereq_step)
        return False, "OLT not found.", steps
    if not allocated_ip:
        message = (
            "A static management IP is required before ACS authorization can be "
            "guaranteed."
        )
        acs_prereq_step.update({"success": False, "message": message})
        steps.append(acs_prereq_step)
        return False, message, steps

    from app.services.network.effective_ont_config import resolve_effective_ont_config

    effective_values = resolve_effective_ont_config(db, ont, olt=olt).get("values", {})
    missing_acs_prereqs: list[str] = []
    if not effective_values.get("tr069_acs_server_id"):
        missing_acs_prereqs.append("ACS server")
    if effective_values.get("tr069_olt_profile_id") in (None, ""):
        missing_acs_prereqs.append("OLT TR-069 profile")
    if effective_values.get("mgmt_vlan") in (None, ""):
        missing_acs_prereqs.append("management VLAN")
    effective_mgmt_ip = effective_values.get("mgmt_ip_address")
    if not effective_mgmt_ip:
        missing_acs_prereqs.append("static management IP")
    elif str(effective_mgmt_ip) != str(allocated_ip):
        missing_acs_prereqs.append("matching static management IP")

    if missing_acs_prereqs:
        message = (
            "ACS authorization cannot be guaranteed; missing "
            f"{', '.join(missing_acs_prereqs)}."
        )
        acs_prereq_step.update(
            {
                "success": False,
                "message": message,
                "missing": missing_acs_prereqs,
            }
        )
        steps.append(acs_prereq_step)
        return False, message, steps

    steps.append(acs_prereq_step)

    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning(
            "Failed to commit before ACS foundation for ONT %s",
            serial_number,
            exc_info=True,
        )
        return False, f"Database commit failed: {exc}", steps

    try:
        acs_result = apply_acs_foundation(
            db,
            ont_unit_id=ont_unit_id,
            olt_id=olt_id,
            fsp=fsp,
            ont_id_on_olt=ont_id_on_olt,
            wait_for_acs_bootstrap=True,
        )
        acs_ok = bool(acs_result.get("success"))
        acs_msg = str(acs_result.get("message") or "")
        steps.append(
            {
                "name": "Apply ACS foundation",
                "success": acs_ok,
                "message": acs_msg,
                "data": acs_result,
            }
        )
        if not acs_ok:
            return (
                False,
                "Authorization foundation failed: ACS foundation was not applied.",
                steps,
            )
    except Exception as exc:
        steps.append(
            {
                "name": "Apply ACS foundation",
                "success": False,
                "message": str(exc),
            }
        )
        logger.warning(
            "Error applying ACS foundation for ONT %s: %s", serial_number, exc
        )
        return False, f"ACS foundation failed: {exc}", steps

    return True, "Authorization foundation completed with ACS connected.", steps


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
    adapter = get_protocol_adapter(olt)

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
    auth_result = adapter.authorize_ont(
        fsp,
        normalized_serial,
        line_profile_id=authorization_profiles.line_profile_id,
        service_profile_id=authorization_profiles.service_profile_id,
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

    # Resolve autofind candidate
    _resolve_authorized_autofind_candidate(
        db, olt_id=olt_id, fsp=fsp, serial_number=normalized_serial
    )

    activation_message = (
        f"{getattr(authorization_profiles, 'message', '')} "
        f"{auth_result.message} {create_msg}".strip()
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
) -> AuthorizationWorkflowResult:
    """Authorize ONT, allocate management IP, apply ACS foundation, and audit log.

    This is the main entry point for ONT authorization. It:
    1. Registers the ONT serial on the OLT
    2. Creates/updates the OntUnit record
    3. Links to PON port
    4. Allocates management IP (if pool configured)
    5. Applies ACS foundation (if mgmt IP allocated)
    6. Logs the action for audit
    """
    started_at = monotonic()

    # Step 1: Core OLT authorization
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

    # Commit OLT authorization before slower follow-up work
    db.commit()

    # Step 2-4: Link PON port, allocate IP, apply ACS foundation
    if result.ont_unit_id and result.ont_id_on_olt is not None:
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

        failed_foundation_message = next(
            (
                str(step.get("message") or "")
                for step in foundation_steps
                if not bool(step.get("success")) and step.get("message")
            ),
            foundation_msg,
        )
        foundation_step_message = (
            failed_foundation_message if not foundation_ok else foundation_msg
        )

        # Add a summary step for the foundation work
        result.steps.append(
            AuthorizationStepResult(
                step=len(result.steps) + 1,
                name="Bring ONT onto ACS",
                success=foundation_ok,
                message=foundation_step_message,
            )
        )

        if not foundation_ok:
            result.success = False
            result.status = "error"
            result.partial_success = True
            result.message = (
                f"ONT authorized, but ACS foundation setup failed: {foundation_msg}"
            )
        else:
            # Check if we got a management IP
            ip_step = next(
                (
                    s
                    for s in foundation_steps
                    if s.get("name") == "Allocate management IP"
                ),
                None,
            )
            allocated_ip = ip_step.get("allocated_ip") if ip_step else None
            if allocated_ip:
                result.message = (
                    f"ONT authorized with management IP {allocated_ip} and ACS connected."
                )
            else:
                result.message = "ONT authorized and ACS connected."

    db.commit()
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
