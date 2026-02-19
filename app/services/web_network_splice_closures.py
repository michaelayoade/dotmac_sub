"""Service helpers for admin fiber splice-closure web routes."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.network import (
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
)
from app.schemas.network import FiberSpliceCreate, FiberSpliceUpdate
from app.services import network as network_service

logger = logging.getLogger(__name__)


def _form_str(form: Mapping[str, object], key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value.strip() if isinstance(value, str) else default


def list_page_data(db: Session) -> dict[str, object]:
    """Return splice closure list and summary stats."""
    closures = db.scalars(
        select(FiberSpliceClosure)
        .where(FiberSpliceClosure.is_active.is_(True))
        .order_by(FiberSpliceClosure.name)
        .limit(200)
    ).all()
    return {"closures": closures, "stats": {"total": len(closures)}}


def get_closure(db: Session, closure_id: str) -> FiberSpliceClosure | None:
    """Return splice closure by id."""
    return db.scalars(
        select(FiberSpliceClosure).where(FiberSpliceClosure.id == closure_id)
    ).first()


def build_form_context(
    *,
    closure: FiberSpliceClosure | dict[str, object] | None,
    action_url: str,
    error: str | None = None,
) -> dict[str, object]:
    """Build shared template context for closure forms."""
    context: dict[str, object] = {"closure": closure, "action_url": action_url}
    if error:
        context["error"] = error
    return context


def parse_form_values(form: FormData) -> dict[str, object]:
    """Parse closure form fields into normalized values."""
    return {
        "name": _form_str(form, "name").strip(),
        "latitude_raw": _form_str(form, "latitude").strip(),
        "longitude_raw": _form_str(form, "longitude").strip(),
        "notes": (_form_str(form, "notes").strip() or None),
        "is_active": _form_str(form, "is_active") == "true",
    }


def validate_name(values: dict[str, object]) -> str | None:
    """Validate required closure fields."""
    if not values.get("name"):
        return "Closure name is required"
    return None


def parse_coordinates(values: dict[str, object]) -> tuple[float | None, float | None]:
    """Parse optional coordinate fields into floats."""
    latitude_raw = str(values.get("latitude_raw") or "")
    longitude_raw = str(values.get("longitude_raw") or "")
    try:
        latitude = float(latitude_raw) if latitude_raw else None
    except ValueError:
        latitude = None
    try:
        longitude = float(longitude_raw) if longitude_raw else None
    except ValueError:
        longitude = None
    return latitude, longitude


def create_closure(db: Session, values: dict[str, object]) -> FiberSpliceClosure:
    """Create and persist a splice closure."""
    latitude, longitude = parse_coordinates(values)
    closure = FiberSpliceClosure(
        name=values["name"],
        latitude=latitude,
        longitude=longitude,
        notes=values.get("notes"),
        is_active=bool(values.get("is_active")),
    )
    db.add(closure)
    db.commit()
    db.refresh(closure)
    return closure


def update_closure(closure: FiberSpliceClosure, values: dict[str, object]) -> None:
    """Apply form values to an existing splice closure."""
    latitude, longitude = parse_coordinates(values)
    closure.name = cast(str, values["name"])
    closure.latitude = latitude
    closure.longitude = longitude
    closure.notes = cast(str | None, values.get("notes"))
    closure.is_active = bool(values.get("is_active"))


def commit_closure_update(db: Session, closure: FiberSpliceClosure, values: dict[str, object]) -> None:
    """Apply form values and flush the closure update."""
    update_closure(closure, values)
    db.flush()


def detail_page_data(db: Session, closure_id: str) -> dict[str, object] | None:
    """Return closure detail payload including trays/splices."""
    closure = get_closure(db, closure_id)
    if not closure:
        return None
    trays = db.scalars(
        select(FiberSpliceTray)
        .where(FiberSpliceTray.closure_id == closure.id)
        .order_by(FiberSpliceTray.tray_number)
    ).all()
    splices = db.scalars(
        select(FiberSplice).where(FiberSplice.closure_id == closure.id)
    ).all()
    return {"closure": closure, "trays": trays, "splices": splices}


def get_tray(db: Session, closure_id: str, tray_id: str) -> FiberSpliceTray | None:
    """Return tray by id under a closure."""
    return db.scalars(
        select(FiberSpliceTray)
        .where(
            FiberSpliceTray.id == tray_id,
            FiberSpliceTray.closure_id == closure_id,
        )
    ).first()


def build_tray_form_context(
    *,
    closure: FiberSpliceClosure,
    tray: FiberSpliceTray | dict[str, object] | None,
    action_url: str,
    error: str | None = None,
) -> dict[str, object]:
    """Build shared context for splice tray forms."""
    context: dict[str, object] = {
        "closure": closure,
        "tray": tray,
        "action_url": action_url,
    }
    if error:
        context["error"] = error
    return context


def parse_tray_form_values(form: FormData) -> dict[str, object]:
    """Parse tray form fields into normalized values."""
    return {
        "tray_number_raw": _form_str(form, "tray_number").strip(),
        "name": _form_str(form, "name").strip(),
        "notes": _form_str(form, "notes").strip(),
    }


def validate_tray_form_values(values: dict[str, object]) -> tuple[int | None, str | None]:
    """Validate tray number field."""
    try:
        tray_number = int(str(values.get("tray_number_raw") or ""))
    except ValueError:
        return None, "Tray number must be a valid integer."
    if tray_number <= 0:
        return None, "Tray number must be greater than 0."
    return tray_number, None


def tray_form_data(values: dict[str, object], *, tray_id: str | None = None) -> dict[str, object]:
    """Build tray-like data for form re-render after errors."""
    data: dict[str, object] = {
        "tray_number": values.get("tray_number_raw"),
        "name": values.get("name"),
        "notes": values.get("notes") or None,
    }
    if tray_id:
        data["id"] = tray_id
    return data


def create_tray(db: Session, closure_id: str, values: dict[str, object]) -> FiberSpliceTray:
    """Create and persist a tray for the closure."""
    tray_number, error = validate_tray_form_values(values)
    if error:
        raise ValueError(error)
    tray = FiberSpliceTray(
        closure_id=closure_id,
        tray_number=tray_number,
        name=(str(values.get("name") or "") or None),
        notes=(str(values.get("notes") or "") or None),
    )
    db.add(tray)
    db.commit()
    db.refresh(tray)
    return tray


def update_tray(tray: FiberSpliceTray, values: dict[str, object]) -> None:
    """Apply form values to an existing tray."""
    tray_number, error = validate_tray_form_values(values)
    if error:
        raise ValueError(error)
    tray.tray_number = cast(int, tray_number)
    tray.name = (str(values.get("name") or "") or None)
    tray.notes = (str(values.get("notes") or "") or None)


def commit_tray_update(db: Session, tray: FiberSpliceTray, values: dict[str, object]) -> None:
    """Apply form values and flush the tray update."""
    update_tray(tray, values)
    db.flush()


def splice_form_dependencies(db: Session, closure_id: str) -> dict[str, object] | None:
    """Return closure plus tray/strand options for splice forms."""
    closure = get_closure(db, closure_id)
    if not closure:
        return None
    trays = db.scalars(
        select(FiberSpliceTray)
        .where(FiberSpliceTray.closure_id == closure.id)
        .order_by(FiberSpliceTray.tray_number)
    ).all()
    strands = db.scalars(
        select(FiberStrand)
        .order_by(FiberStrand.cable_name, FiberStrand.strand_number)
        .limit(500)
    ).all()
    return {"closure": closure, "trays": trays, "strands": strands}


def get_splice(db: Session, closure_id: str, splice_id: str) -> FiberSplice | None:
    """Return splice by id under a closure."""
    return db.scalars(
        select(FiberSplice)
        .where(
            FiberSplice.id == splice_id,
            FiberSplice.closure_id == closure_id,
        )
    ).first()


def build_splice_form_context(
    *,
    closure: FiberSpliceClosure,
    trays: list[FiberSpliceTray],
    strands: list[FiberStrand],
    splice: FiberSplice | dict[str, object] | None,
    action_url: str,
    error: str | None = None,
) -> dict[str, object]:
    """Build shared template context for splice forms."""
    context: dict[str, object] = {
        "closure": closure,
        "trays": trays,
        "strands": strands,
        "splice": splice,
        "action_url": action_url,
    }
    if error:
        context["error"] = error
    return context


def parse_splice_form_values(form: FormData) -> dict[str, object]:
    """Parse splice form fields into normalized values."""
    return {
        "from_strand_id": _form_str(form, "from_strand_id").strip(),
        "to_strand_id": _form_str(form, "to_strand_id").strip(),
        "tray_id": _form_str(form, "tray_id").strip(),
        "splice_type": _form_str(form, "splice_type").strip(),
        "loss_db_raw": _form_str(form, "loss_db").strip(),
        "notes": _form_str(form, "notes").strip(),
    }


def validate_splice_form_values(values: dict[str, object]) -> tuple[float | None, str | None]:
    """Validate splice requirements and parse optional loss value."""
    from_strand_id = str(values.get("from_strand_id") or "")
    to_strand_id = str(values.get("to_strand_id") or "")
    loss_db_raw = str(values.get("loss_db_raw") or "")
    if not from_strand_id or not to_strand_id:
        return None, "Both from and to strands are required."
    if from_strand_id == to_strand_id:
        return None, "From and to strands must be different."
    loss_db = None
    if loss_db_raw:
        try:
            loss_db = float(loss_db_raw)
        except ValueError:
            return None, "Loss must be a valid number."
    return loss_db, None


def splice_form_data(values: dict[str, object], *, splice_id: str | None = None) -> dict[str, object]:
    """Build splice-like data for form re-render after errors."""
    data: dict[str, object] = {
        "from_strand_id": values.get("from_strand_id"),
        "to_strand_id": values.get("to_strand_id"),
        "tray_id": values.get("tray_id") or None,
        "splice_type": values.get("splice_type") or None,
        "loss_db": values.get("loss_db_raw"),
        "notes": values.get("notes") or None,
    }
    if splice_id:
        data["id"] = splice_id
    return data


def create_splice(db: Session, closure_id: str, values: dict[str, object]) -> FiberSplice:
    """Create a fiber splice under a closure."""
    loss_db, error = validate_splice_form_values(values)
    if error:
        raise ValueError(error)
    payload = FiberSpliceCreate(
        closure_id=cast(UUID, UUID(closure_id)),
        from_strand_id=cast(UUID, UUID(str(values.get("from_strand_id") or ""))),
        to_strand_id=cast(UUID, UUID(str(values.get("to_strand_id") or ""))),
        tray_id=UUID(tid) if (tid := (str(values.get("tray_id") or "") or "").strip()) else None,
        splice_type=(str(values.get("splice_type") or "") or None),
        loss_db=loss_db,
        notes=(str(values.get("notes") or "") or None),
    )
    return cast(FiberSplice, network_service.fiber_splices.create(db=db, payload=payload))


def update_splice(db: Session, splice_id: str, values: dict[str, object]) -> FiberSplice:
    """Update an existing fiber splice."""
    loss_db, error = validate_splice_form_values(values)
    if error:
        raise ValueError(error)
    payload = FiberSpliceUpdate(
        from_strand_id=cast(UUID, UUID(str(values.get("from_strand_id") or ""))),
        to_strand_id=cast(UUID, UUID(str(values.get("to_strand_id") or ""))),
        tray_id=UUID(tid) if (tid := (str(values.get("tray_id") or "") or "").strip()) else None,
        splice_type=(str(values.get("splice_type") or "") or None),
        loss_db=loss_db,
        notes=(str(values.get("notes") or "") or None),
    )
    return cast(
        FiberSplice,
        network_service.fiber_splices.update(
        db=db,
        splice_id=splice_id,
        payload=payload,
        ),
    )
