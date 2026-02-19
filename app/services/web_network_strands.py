"""Service helpers for admin fiber-strand web routes."""

from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Session

from app.schemas.network import FiberStrandCreate, FiberStrandUpdate
from app.services import network as network_service
from app.services.common import validate_enum


def list_page_data(db: Session) -> dict[str, object]:
    """Return strand list and summary stats for index page."""
    strands = (
        network_service.fiber_strands.list(
            db=db,
            cable_name=None,
            status=None,
            order_by="cable_name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
    )
    return {
        "strands": strands,
        "stats": {
            "total": len(strands),
            "available": sum(1 for strand in strands if strand.status.value == "available"),
            "in_use": sum(1 for strand in strands if strand.status.value == "in_use"),
        },
    }


def get_strand(db: Session, strand_id: str):
    """Return a strand by id."""
    return network_service.fiber_strands.get(db=db, strand_id=strand_id)


def build_form_context(
    *,
    strand,
    action_url: str,
    error: str | None = None,
) -> dict[str, object]:
    """Build shared template context for strand forms."""
    context = {
        "strand": strand,
        "action_url": action_url,
    }
    if error:
        context["error"] = error
    return context


def parse_form_values(form) -> dict[str, object]:
    """Parse form values into normalized strings."""
    return {
        "cable_name": form.get("cable_name", "").strip(),
        "strand_number_raw": form.get("strand_number", "").strip(),
        "label": form.get("label", "").strip(),
        "status": form.get("status", "").strip(),
        "upstream_type": form.get("upstream_type", "").strip(),
        "downstream_type": form.get("downstream_type", "").strip(),
        "notes": form.get("notes", "").strip(),
    }


def validate_form_values(values: dict[str, object]) -> tuple[int | None, str | None]:
    """Validate required fields and parse strand number."""
    cable_name = str(values.get("cable_name") or "")
    strand_number_raw = str(values.get("strand_number_raw") or "")
    if not cable_name:
        return None, "Cable name is required."
    try:
        strand_number = int(strand_number_raw)
    except ValueError:
        return None, "Strand number must be a valid integer."
    return strand_number, None


def strand_form_data(values: dict[str, object], *, strand_id: str | None = None) -> dict[str, object]:
    """Build strand-like object for form re-render after errors."""
    data = {
        "cable_name": values.get("cable_name"),
        "strand_number": values.get("strand_number_raw"),
        "label": values.get("label") or None,
        "status": {"value": values.get("status")} if values.get("status") else None,
        "upstream_type": {"value": values.get("upstream_type")} if values.get("upstream_type") else None,
        "downstream_type": {"value": values.get("downstream_type")} if values.get("downstream_type") else None,
        "notes": values.get("notes") or None,
    }
    if strand_id:
        data["id"] = strand_id
    return data


def create_strand(db: Session, values: dict[str, object]):
    """Create strand from parsed values."""
    strand_number, error = validate_form_values(values)
    if error:
        raise ValueError(error)
    from app.models.network import FiberEndpointType, FiberStrandStatus

    payload = FiberStrandCreate(
        cable_name=str(values.get("cable_name") or ""),
        strand_number=cast(int, strand_number),
        label=(str(values.get("label") or "") or None),
        status=validate_enum(
            str(values.get("status") or "available"),
            FiberStrandStatus,
            "status",
        ),
        upstream_type=(
            validate_enum(str(values.get("upstream_type") or ""), FiberEndpointType, "upstream_type")
            if values.get("upstream_type")
            else None
        ),
        downstream_type=(
            validate_enum(
                str(values.get("downstream_type") or ""),
                FiberEndpointType,
                "downstream_type",
            )
            if values.get("downstream_type")
            else None
        ),
        notes=(str(values.get("notes") or "") or None),
    )
    return network_service.fiber_strands.create(db=db, payload=payload)


def update_strand(db: Session, strand_id: str, values: dict[str, object]):
    """Update strand from parsed values."""
    strand_number, error = validate_form_values(values)
    if error:
        raise ValueError(error)
    from app.models.network import FiberEndpointType, FiberStrandStatus

    payload = FiberStrandUpdate(
        cable_name=str(values.get("cable_name") or ""),
        strand_number=strand_number,
        label=(str(values.get("label") or "") or None),
        status=(
            validate_enum(str(values.get("status") or ""), FiberStrandStatus, "status")
            if values.get("status")
            else None
        ),
        upstream_type=(
            validate_enum(str(values.get("upstream_type") or ""), FiberEndpointType, "upstream_type")
            if values.get("upstream_type")
            else None
        ),
        downstream_type=(
            validate_enum(
                str(values.get("downstream_type") or ""),
                FiberEndpointType,
                "downstream_type",
            )
            if values.get("downstream_type")
            else None
        ),
        notes=(str(values.get("notes") or "") or None),
    )
    return network_service.fiber_strands.update(
        db=db,
        strand_id=strand_id,
        payload=payload,
    )


__all__ = [
    "build_form_context",
    "create_strand",
    "get_strand",
    "list_page_data",
    "parse_form_values",
    "strand_form_data",
    "update_strand",
    "validate_form_values",
]
