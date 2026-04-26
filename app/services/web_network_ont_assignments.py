"""Service helpers for admin ONT assignment web routes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.ont_autofind import OltAutofindCandidate
from app.schemas.network import OntAssignmentCreate, OntAssignmentUpdate
from app.services import network as network_service
from app.services import subscriber as subscriber_service
from app.services.common import coerce_uuid
from app.services.network.olt_autofind import parse_fsp_parts
from app.services.network.olt_web_topology import ensure_canonical_pon_port
from app.services.web_network_ont_autofind import (
    _normalize_serial,
    _normalized_serial_expr,
)

logger = logging.getLogger(__name__)


@dataclass
class AssignmentFormResult:
    ont: Any | None = None
    assignment: Any | None = None
    values: dict[str, object] | None = None
    error: str | None = None
    not_found: bool = False
    not_found_message: str = "ONT not found"


def resolve_pon_port_for_ont(db: Session, ont) -> dict[str, object]:
    """Resolve PON port from ONT's board/port or autofind candidate.

    Checks in order:
    1. ONT's board/port fields (set after authorization)
    2. Active autofind candidate (known from discovery before authorization)

    Returns dict with:
        - pon_port_id: UUID if resolved, None otherwise
        - pon_port_label: Human-readable label for display
        - pon_port_resolved: True if auto-resolved
    """
    olt_device_id = getattr(ont, "olt_device_id", None)
    board = getattr(ont, "board", None)
    port = getattr(ont, "port", None)

    # If ONT doesn't have board/port, check autofind candidate
    if not board or not port:
        # First try by ont_unit_id (direct link)
        candidate = db.scalars(
            select(OltAutofindCandidate)
            .where(
                OltAutofindCandidate.ont_unit_id == ont.id,
                OltAutofindCandidate.is_active.is_(True),
            )
            .order_by(OltAutofindCandidate.last_seen_at.desc())
            .limit(1)
        ).first()
        # Fallback to serial number match
        if not candidate:
            normalized_serial = _normalize_serial(ont.serial_number)
            candidate = db.scalars(
                select(OltAutofindCandidate)
                .where(
                    _normalized_serial_expr(OltAutofindCandidate.serial_number)
                    == normalized_serial,
                    OltAutofindCandidate.is_active.is_(True),
                )
                .order_by(OltAutofindCandidate.last_seen_at.desc())
                .limit(1)
            ).first()
        if candidate:
            olt_device_id = candidate.olt_id
            board, port = parse_fsp_parts(candidate.fsp)

    if not olt_device_id or not board or not port:
        return {
            "pon_port_id": None,
            "pon_port_label": None,
            "pon_port_resolved": False,
        }

    fsp = f"{board}/{port}"
    try:
        pon_port = ensure_canonical_pon_port(
            db, olt_id=olt_device_id, fsp=fsp, board=board, port=port
        )
        # Get OLT name for display
        olt_name = ""
        olt_device = getattr(ont, "olt_device", None)
        if not olt_device:
            from app.models.network import OLTDevice

            olt_device = db.get(OLTDevice, olt_device_id)
        if olt_device:
            olt_name = f" ({olt_device.name})"
        return {
            "pon_port_id": str(pon_port.id),
            "pon_port_label": f"{fsp}{olt_name}",
            "pon_port_resolved": True,
        }
    except Exception:
        logger.exception("Failed to resolve PON port for ONT %s", ont.id)
        return {
            "pon_port_id": None,
            "pon_port_label": None,
            "pon_port_resolved": False,
        }


def assignment_form_dependencies(db: Session, ont=None) -> dict[str, object]:
    """Return form context for ONT assignment.

    When ont is provided, auto-resolves PON port from discovered board/port.
    Subscriber accounts are fetched via HTMX typeahead search.
    """
    from app.models.network import Vlan, VlanPurpose

    result: dict[str, object] = {
        # Accounts fetched via HTMX typeahead, not static dropdown
        "accounts": [],
        # Addresses will be resolved from selected subscriber
        "addresses": [],
        # Subscriptions will be resolved from selected subscriber
        "subscriptions": [],
    }

    # Auto-resolve PON port from ONT discovery data
    if ont is not None:
        result.update(resolve_pon_port_for_ont(db, ont))

    else:
        result["pon_port_id"] = None
        result["pon_port_label"] = None
        result["pon_port_resolved"] = False

    return result


def get_available_mgmt_ips_for_vlan(
    db: Session, vlan_id: str | None, limit: int = 50
) -> list[dict[str, str]]:
    """Return available management IP addresses from the VLAN's IP pool.

    Source of truth:
    - VLAN has ip_pools relationship
    - IpPool contains IPv4Address records
    - Available = ont_unit_id IS NULL and is_reserved = False
    """
    from app.models.network import IpPool, IPv4Address, Vlan

    if not vlan_id:
        return []

    vlan_uuid = coerce_uuid(vlan_id)
    if not vlan_uuid:
        return []

    # Find IP pools linked to this VLAN
    pools = db.scalars(
        select(IpPool).where(
            IpPool.vlan_id == vlan_uuid,
            IpPool.is_active.is_(True),
        )
    ).all()

    if not pools:
        return []

    pool_ids = [p.id for p in pools]

    # Get available IPs from those pools
    available_ips = db.scalars(
        select(IPv4Address)
        .where(
            IPv4Address.pool_id.in_(pool_ids),
            IPv4Address.ont_unit_id.is_(None),
            IPv4Address.is_reserved.is_(False),
        )
        .order_by(IPv4Address.address)
        .limit(limit)
    ).all()

    return [
        {"address": ip.address, "id": str(ip.id)}
        for ip in available_ips
    ]


def parse_form_values(form) -> dict[str, object]:
    """Parse ONT assignment form values including service config."""
    return {
        "pon_port_id": form.get("pon_port_id", "").strip(),
        "account_id": form.get("account_id", "").strip() or None,
        "service_address_id": form.get("service_address_id", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
        # Service configuration fields
        "wan_mode": form.get("wan_mode", "").strip() or None,
        "ip_mode": form.get("ip_mode", "").strip() or None,
        "static_ip": form.get("static_ip", "").strip() or None,
        "static_gateway": form.get("static_gateway", "").strip() or None,
        "static_subnet": form.get("static_subnet", "").strip() or None,
        "pppoe_username": form.get("pppoe_username", "").strip() or None,
        "pppoe_password": form.get("pppoe_password", "").strip() or None,
        "wifi_ssid": form.get("wifi_ssid", "").strip() or None,
        "wifi_password": form.get("wifi_password", "").strip() or None,
        # Management IP configuration
        "mgmt_ip_mode": form.get("mgmt_ip_mode", "").strip() or None,
        "mgmt_ip_address": form.get("mgmt_ip_address", "").strip() or None,
    }


def validate_form_values(values: dict[str, object]) -> str | None:
    """Validate assignment fields.

    Subscriber account is optional: ONT assignments may be created without
    a subscriber ref for standalone network operations (see DCP-11).
    ``service_address_id`` requires ``account_id`` because addresses belong
    to a subscriber.
    """
    if values.get("service_address_id") and not values.get("account_id"):
        return "Service address requires a subscriber account"
    return None


def has_active_assignment(db: Session, ont_id: str) -> bool:
    """Return True when ONT already has an active assignment."""
    assignments = network_service.ont_assignments.list(
        db=db,
        ont_unit_id=ont_id,
        pon_port_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=20,
        offset=0,
    )
    return any(a.active for a in assignments)


def resolve_pon_port_id_for_assignment(
    db: Session, ont, values: dict[str, object]
) -> str | None:
    """Resolve PON port ID for assignment, auto-detecting from ONT if possible."""
    # Use explicitly provided value first
    if values.get("pon_port_id"):
        return str(values["pon_port_id"])

    # Auto-resolve from ONT's discovered board/port
    resolved = resolve_pon_port_for_ont(db, ont)
    if resolved.get("pon_port_id"):
        return str(resolved["pon_port_id"])

    # For TR-069-only devices, PON port is optional
    return None


def resolve_service_address_for_subscriber(
    db: Session, subscriber_id: str
) -> str | None:
    """Get the subscriber's primary/first service address."""
    addresses = subscriber_service.addresses.list(
        db=db,
        subscriber_id=subscriber_id,
        order_by="created_at",
        order_dir="asc",
        limit=1,
        offset=0,
    )
    if addresses:
        return str(addresses[0].id)
    return None


def create_assignment(db: Session, ont, values: dict[str, object]) -> None:
    """Create ONT assignment, auto-resolving PON port and address.

    Subscriber account is optional. When absent, the service address lookup
    is skipped and the assignment is created without a subscriber ref.
    Service config fields (WAN mode, IP mode, PPPoE, WiFi) are saved for
    provisioning use.
    """
    pon_port_id_str = resolve_pon_port_id_for_assignment(db, ont, values)
    pon_port_id = coerce_uuid(pon_port_id_str) if pon_port_id_str else None

    subscriber_id_str = str(values["account_id"]) if values.get("account_id") else None

    service_address_id_str: str | None
    if values.get("service_address_id"):
        service_address_id_str = str(values["service_address_id"])
    elif subscriber_id_str is not None:
        service_address_id_str = resolve_service_address_for_subscriber(
            db, subscriber_id_str
        )
    else:
        service_address_id_str = None

    payload = OntAssignmentCreate(
        ont_unit_id=ont.id,
        pon_port_id=pon_port_id,
        subscriber_id=coerce_uuid(subscriber_id_str) if subscriber_id_str else None,
        service_address_id=(
            coerce_uuid(service_address_id_str) if service_address_id_str else None
        ),
        assigned_at=datetime.now(UTC),
        active=True,
        notes=str(values.get("notes")) if values.get("notes") else None,
        # Service configuration
        wan_mode=str(values.get("wan_mode")) if values.get("wan_mode") else "routing",
        ip_mode=str(values.get("ip_mode")) if values.get("ip_mode") else "dhcp",
        static_ip=str(values.get("static_ip")) if values.get("static_ip") else None,
        static_gateway=str(values.get("static_gateway")) if values.get("static_gateway") else None,
        static_subnet=str(values.get("static_subnet")) if values.get("static_subnet") else None,
        pppoe_username=str(values.get("pppoe_username")) if values.get("pppoe_username") else None,
        pppoe_password=str(values.get("pppoe_password")) if values.get("pppoe_password") else None,
        wifi_ssid=str(values.get("wifi_ssid")) if values.get("wifi_ssid") else None,
        wifi_password=str(values.get("wifi_password")) if values.get("wifi_password") else None,
        # Management IP configuration
        mgmt_ip_mode=str(values.get("mgmt_ip_mode")) if values.get("mgmt_ip_mode") else "inactive",
        mgmt_ip_address=str(values.get("mgmt_ip_address")) if values.get("mgmt_ip_address") else None,
    )
    network_service.ont_assignments.create(db=db, payload=payload)


def form_payload(
    values: dict[str, object], db: Session | None = None
) -> dict[str, object]:
    """Return template-friendly form payload.

    If db is provided and account_id is set, looks up the subscriber label
    so the typeahead field can be repopulated on validation errors.
    """
    result = {
        "pon_port_id": values.get("pon_port_id"),
        "account_id": values.get("account_id"),
        "account_label": "",
        "service_address_id": values.get("service_address_id"),
        "notes": values.get("notes"),
        # Service config fields
        "wan_mode": values.get("wan_mode"),
        "ip_mode": values.get("ip_mode"),
        "static_ip": values.get("static_ip"),
        "static_gateway": values.get("static_gateway"),
        "static_subnet": values.get("static_subnet"),
        "pppoe_username": values.get("pppoe_username"),
        "pppoe_password": values.get("pppoe_password"),
        "wifi_ssid": values.get("wifi_ssid"),
        "wifi_password": values.get("wifi_password"),
        # Management IP config fields
        "mgmt_ip_mode": values.get("mgmt_ip_mode"),
        "mgmt_ip_address": values.get("mgmt_ip_address"),
    }
    # Look up subscriber label for typeahead repopulation
    if db and values.get("account_id"):
        try:
            subscriber = subscriber_service.subscribers.get(
                db, str(values["account_id"])
            )
            if subscriber:
                if subscriber.category == subscriber.category.business:
                    result["account_label"] = (
                        subscriber.company_name
                        or subscriber.display_name
                        or subscriber.full_name
                    )
                else:
                    label = f"{subscriber.first_name} {subscriber.last_name}"
                    if subscriber.email:
                        label = f"{label} ({subscriber.email})"
                    result["account_label"] = label
        except Exception:
            pass  # Keep empty label on lookup failure
    return result


def assignment_form_payload_from_assignment(assignment) -> dict[str, object]:
    """Return template-friendly form defaults for an existing assignment."""
    subscriber = getattr(assignment, "subscriber", None)
    account_label = getattr(subscriber, "name", "") if subscriber else ""
    # Get enum values as strings for form display
    wan_mode_val = getattr(assignment, "wan_mode", None)
    ip_mode_val = getattr(assignment, "ip_mode", None)
    mgmt_ip_mode_val = getattr(assignment, "mgmt_ip_mode", None)
    return {
        "pon_port_id": str(assignment.pon_port_id) if assignment.pon_port_id else "",
        "account_id": str(assignment.subscriber_id) if assignment.subscriber_id else "",
        "account_label": account_label,
        "service_address_id": (
            str(assignment.service_address_id) if assignment.service_address_id else ""
        ),
        "notes": assignment.notes or "",
        # Service config fields
        "wan_mode": wan_mode_val.value if wan_mode_val else "routing",
        "ip_mode": ip_mode_val.value if ip_mode_val else "dhcp",
        "static_ip": getattr(assignment, "static_ip", "") or "",
        "static_gateway": getattr(assignment, "static_gateway", "") or "",
        "static_subnet": getattr(assignment, "static_subnet", "") or "",
        "pppoe_username": getattr(assignment, "pppoe_username", "") or "",
        "pppoe_password": getattr(assignment, "pppoe_password", "") or "",
        "wifi_ssid": getattr(assignment, "wifi_ssid", "") or "",
        "wifi_password": getattr(assignment, "wifi_password", "") or "",
        # Management IP config fields
        "mgmt_ip_mode": mgmt_ip_mode_val.value if mgmt_ip_mode_val else "inactive",
        "mgmt_ip_address": getattr(assignment, "mgmt_ip_address", "") or "",
    }


def get_ont_for_assignment_form(db: Session, ont_id: str) -> AssignmentFormResult:
    """Load the ONT for a new assignment form."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return AssignmentFormResult(not_found=True, not_found_message="ONT not found")
    return AssignmentFormResult(ont=ont)


def get_assignment_edit_form(
    db: Session,
    *,
    ont_id: str,
    assignment_id: str,
) -> AssignmentFormResult:
    """Load an existing ONT assignment for the edit form."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return AssignmentFormResult(not_found=True, not_found_message="ONT not found")

    try:
        assignment = network_service.ont_assignments.get(db, assignment_id)
    except HTTPException:
        return AssignmentFormResult(
            not_found=True,
            not_found_message="Assignment not found for this ONT",
        )

    if str(assignment.ont_unit_id) != str(ont.id):
        return AssignmentFormResult(
            not_found=True,
            not_found_message="Assignment not found for this ONT",
        )

    return AssignmentFormResult(
        ont=ont,
        assignment=assignment,
        values=assignment_form_payload_from_assignment(assignment),
    )


def create_assignment_from_form(db: Session, ont_id: str, form) -> AssignmentFormResult:
    """Validate and create an ONT assignment from submitted web form data."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return AssignmentFormResult(not_found=True)

    values = parse_form_values(form)
    error = validate_form_values(values)
    if not error and has_active_assignment(db, ont_id):
        error = "This ONT is already assigned"
    if error:
        return AssignmentFormResult(ont=ont, values=values, error=error)

    try:
        create_assignment(db, ont, values)
    except IntegrityError as exc:
        db.rollback()
        msg = (
            "This ONT is already assigned. Refresh the page and try again."
            if "ix_ont_assignments_active_unit" in str(exc)
            else "Could not create assignment due to a data conflict."
        )
        return AssignmentFormResult(ont=ont, values=values, error=msg)
    return AssignmentFormResult(ont=ont, values=values)


def update_assignment_from_form(
    db: Session,
    *,
    ont_id: str,
    assignment_id: str,
    form,
) -> AssignmentFormResult:
    """Validate and update an ONT assignment from submitted web form data."""
    loaded = get_assignment_edit_form(db, ont_id=ont_id, assignment_id=assignment_id)
    if loaded.not_found:
        return loaded

    assignment = loaded.assignment
    values = parse_form_values(form)
    error = validate_form_values(values)
    resolved_pon_port_id = (
        coerce_uuid(str(values["pon_port_id"]))
        if values.get("pon_port_id")
        else getattr(assignment, "pon_port_id", None)
    )
    if resolved_pon_port_id is None:
        error = error or "PON port is required"

    if error:
        return AssignmentFormResult(
            ont=loaded.ont,
            assignment=assignment,
            values=values,
            error=error,
        )

    payload = OntAssignmentUpdate(
        pon_port_id=resolved_pon_port_id,
        subscriber_id=(
            coerce_uuid(str(values["account_id"])) if values.get("account_id") else None
        ),
        service_address_id=(
            coerce_uuid(str(values["service_address_id"]))
            if values.get("service_address_id")
            else None
        ),
        notes=str(values.get("notes")) if values.get("notes") else None,
        # Service configuration
        wan_mode=str(values.get("wan_mode")) if values.get("wan_mode") else None,
        ip_mode=str(values.get("ip_mode")) if values.get("ip_mode") else None,
        static_ip=str(values.get("static_ip")) if values.get("static_ip") else None,
        static_gateway=str(values.get("static_gateway")) if values.get("static_gateway") else None,
        static_subnet=str(values.get("static_subnet")) if values.get("static_subnet") else None,
        pppoe_username=str(values.get("pppoe_username")) if values.get("pppoe_username") else None,
        pppoe_password=str(values.get("pppoe_password")) if values.get("pppoe_password") else None,
        wifi_ssid=str(values.get("wifi_ssid")) if values.get("wifi_ssid") else None,
        wifi_password=str(values.get("wifi_password")) if values.get("wifi_password") else None,
        # Management IP configuration
        mgmt_ip_mode=str(values.get("mgmt_ip_mode")) if values.get("mgmt_ip_mode") else None,
        mgmt_ip_address=str(values.get("mgmt_ip_address")) if values.get("mgmt_ip_address") else None,
    )
    network_service.ont_assignments.update(db, assignment_id, payload)
    return AssignmentFormResult(ont=loaded.ont, assignment=assignment, values=values)


def remove_assignment(
    db: Session,
    *,
    ont_id: str,
    assignment_id: str,
) -> AssignmentFormResult:
    """Validate and remove an ONT assignment."""
    loaded = get_assignment_edit_form(db, ont_id=ont_id, assignment_id=assignment_id)
    if loaded.not_found:
        return loaded

    network_service.ont_assignments.delete(db, assignment_id)
    return loaded
