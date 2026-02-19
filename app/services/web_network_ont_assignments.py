"""Service helpers for admin ONT assignment web routes."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.schemas.network import OntAssignmentCreate, OntUnitUpdate
from app.services import catalog as catalog_service
from app.services import network as network_service
from app.services import subscriber as subscriber_service
from app.services.common import coerce_uuid


def assignment_form_dependencies(db: Session) -> dict[str, object]:
    """Return common select options for ONT assignment form."""
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
        "accounts": subscriber_service.accounts.list(
            db=db,
            subscriber_id=None,
            reseller_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=500,
            offset=0,
        ),
        "subscriptions": catalog_service.subscriptions.list(
            db=db,
            subscriber_id=None,
            offer_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=500,
            offset=0,
        ),
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
    if not values.get("pon_port_id"):
        return "PON port is required"
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
    payload = OntAssignmentCreate(
        ont_unit_id=ont.id,
        pon_port_id=coerce_uuid(str(values["pon_port_id"])),
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
        assigned_at=datetime.now(UTC),
        active=True,
        notes=str(values.get("notes")) if values.get("notes") else None,
    )
    network_service.ont_assignments.create(db=db, payload=payload)
    network_service.ont_units.update(
        db=db,
        unit_id=str(ont.id),
        payload=OntUnitUpdate(is_active=True),
    )


def form_payload(values: dict[str, object]) -> dict[str, object]:
    """Return template-friendly form payload."""
    return {
        "pon_port_id": values.get("pon_port_id"),
        "account_id": values.get("account_id"),
        "subscription_id": values.get("subscription_id"),
        "service_address_id": values.get("service_address_id"),
        "notes": values.get("notes"),
    }
