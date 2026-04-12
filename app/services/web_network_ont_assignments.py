"""Service helpers for admin ONT assignment web routes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.schemas.network import OntAssignmentCreate, OntAssignmentUpdate
from app.services import network as network_service
from app.services import subscriber as subscriber_service
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


@dataclass
class AssignmentFormResult:
    ont: Any | None = None
    assignment: Any | None = None
    values: dict[str, object] | None = None
    error: str | None = None
    not_found: bool = False
    not_found_message: str = "ONT not found"


def assignment_form_dependencies(db: Session) -> dict[str, object]:
    """Return common select options for ONT assignment form.

    Note: ONT assignments link directly to subscribers, not subscriptions.
    This enables independent OLT management without requiring subscription context.
    """
    return {
        "pon_ports": network_service.pon_ports.list(
            db=db,
            olt_id=None,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=500,
            offset=0,
        ),
        # Accounts are now fetched via HTMX typeahead search instead
        # of loading all 500 into a static <select> dropdown.
        "accounts": [],
        "addresses": subscriber_service.addresses.list(
            db=db,
            subscriber_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=500,
            offset=0,
        ),
    }


def parse_form_values(form) -> dict[str, object]:
    """Parse ONT assignment form values."""
    return {
        "pon_port_id": form.get("pon_port_id", "").strip(),
        "account_id": form.get("account_id", "").strip() or None,
        "subscription_id": form.get("subscription_id", "").strip() or None,
        "service_address_id": form.get("service_address_id", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
    }


def validate_form_values(values: dict[str, object]) -> str | None:
    """Validate required assignment fields."""
    if not values.get("account_id"):
        return "Subscriber account is required"
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


def create_assignment(db: Session, ont, values: dict[str, object]) -> None:
    """Create ONT assignment and activate ONT."""
    resolved_pon_port_id = (
        coerce_uuid(str(values["pon_port_id"]))
        if values.get("pon_port_id")
        else getattr(ont, "pon_port_id", None)
    )
    if resolved_pon_port_id is None:
        raise ValueError("PON port is required")

    payload = OntAssignmentCreate(
        ont_unit_id=ont.id,
        pon_port_id=resolved_pon_port_id,
        subscriber_id=coerce_uuid(str(values["account_id"])),
        service_address_id=(
            coerce_uuid(str(values["service_address_id"]))
            if values.get("service_address_id")
            else None
        ),
        assigned_at=datetime.now(UTC),
        active=True,
        notes=str(values.get("notes")) if values.get("notes") else None,
    )
    network_service.ont_assignments.create(db=db, payload=payload)


def form_payload(values: dict[str, object]) -> dict[str, object]:
    """Return template-friendly form payload."""
    return {
        "pon_port_id": values.get("pon_port_id"),
        "account_id": values.get("account_id"),
        "subscription_id": values.get("subscription_id"),
        "service_address_id": values.get("service_address_id"),
        "notes": values.get("notes"),
    }


def assignment_form_payload_from_assignment(assignment) -> dict[str, object]:
    """Return template-friendly form defaults for an existing assignment."""
    subscriber = getattr(assignment, "subscriber", None)
    account_label = getattr(subscriber, "name", "") if subscriber else ""
    return {
        "pon_port_id": str(assignment.pon_port_id) if assignment.pon_port_id else "",
        "account_id": str(assignment.subscriber_id) if assignment.subscriber_id else "",
        "account_label": account_label,
        "subscription_id": (
            str(assignment.subscription_id) if assignment.subscription_id else ""
        ),
        "service_address_id": (
            str(assignment.service_address_id) if assignment.service_address_id else ""
        ),
        "notes": assignment.notes or "",
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
        subscriber_id=coerce_uuid(str(values["account_id"])),
        subscription_id=(
            coerce_uuid(str(values["subscription_id"]))
            if values.get("subscription_id")
            else None
        ),
        service_address_id=(
            coerce_uuid(str(values["service_address_id"]))
            if values.get("service_address_id")
            else None
        ),
        notes=str(values.get("notes")) if values.get("notes") else None,
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
