"""Service helpers for admin fiber FDH cabinet pages."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.network import FdhCabinet, Splitter
from app.services.audit_helpers import diff_dicts, model_to_dict
from app.services import catalog as catalog_service
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.models.catalog import RegionZone


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value.strip() if isinstance(value, str) else default


def list_page_data(db: Session) -> dict[str, object]:
    """Return FDH cabinet list and summary stats."""
    cabinets = db.scalars(
        select(FdhCabinet)
        .where(FdhCabinet.is_active.is_(True))
        .order_by(FdhCabinet.name)
        .limit(200)
    ).all()
    return {"cabinets": cabinets, "stats": {"total": len(cabinets)}}


def regions_for_forms(db: Session) -> list:
    """Return active region zones for form select options."""
    return cast(
        list["RegionZone"],
        catalog_service.region_zones.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
        ),
    )


def get_cabinet(db: Session, cabinet_id: str) -> FdhCabinet | None:
    """Get FDH cabinet by id."""
    return db.scalars(
        select(FdhCabinet).where(FdhCabinet.id == cabinet_id)
    ).first()


def build_form_context(
    db: Session,
    *,
    cabinet: FdhCabinet | None,
    action_url: str,
    error: str | None = None,
) -> dict[str, object]:
    """Build shared form context for create/edit templates."""
    context: dict[str, object] = {
        "cabinet": cabinet,
        "regions": regions_for_forms(db),
        "action_url": action_url,
    }
    if error:
        context["error"] = error
    return context


def parse_form_values(form: FormData) -> dict[str, object]:
    """Parse FDH cabinet form fields into normalized values."""
    return {
        "name": _form_str(form, "name"),
        "code": (_form_str(form, "code") or None),
        "region_id": (_form_str(form, "region_id") or None),
        "latitude_raw": _form_str(form, "latitude"),
        "longitude_raw": _form_str(form, "longitude"),
        "notes": (_form_str(form, "notes") or None),
        "is_active": _form_str(form, "is_active") == "true",
    }


def validate_name(name: str) -> str | None:
    """Validate required name field."""
    if not name:
        return "Cabinet name is required"
    return None


def parse_coordinates(latitude_raw: str, longitude_raw: str) -> tuple[float | None, float | None]:
    """Parse optional latitude/longitude strings into floats."""
    try:
        latitude = float(latitude_raw) if latitude_raw else None
    except ValueError:
        latitude = None
    try:
        longitude = float(longitude_raw) if longitude_raw else None
    except ValueError:
        longitude = None
    return latitude, longitude


def create_cabinet(db: Session, values: dict[str, object]) -> FdhCabinet:
    """Create and persist a cabinet from parsed values."""
    latitude, longitude = parse_coordinates(
        str(values.get("latitude_raw") or ""),
        str(values.get("longitude_raw") or ""),
    )
    cabinet = FdhCabinet(
        name=values["name"],
        code=values.get("code"),
        region_id=values.get("region_id"),
        latitude=latitude,
        longitude=longitude,
        notes=values.get("notes"),
        is_active=bool(values.get("is_active")),
    )
    db.add(cabinet)
    db.commit()
    db.refresh(cabinet)
    return cabinet


def create_cabinet_submission(
    db: Session,
    form: FormData,
    *,
    action_url: str,
) -> dict[str, object]:
    """Handle FDH cabinet create form parsing/validation/create."""
    values = parse_form_values(form)
    error = validate_name(str(values["name"]))
    if error:
        return {
            "cabinet": None,
            "error": error,
            "form_context": build_form_context(
                db,
                cabinet=None,
                action_url=action_url,
                error=error,
            ),
        }
    cabinet = create_cabinet(db, values)
    return {"cabinet": cabinet, "error": None, "form_context": None}


def update_cabinet(cabinet: FdhCabinet, values: dict[str, object]) -> None:
    """Apply parsed form values to an existing cabinet."""
    latitude, longitude = parse_coordinates(
        str(values.get("latitude_raw") or ""),
        str(values.get("longitude_raw") or ""),
    )
    cabinet.name = cast(str, values["name"])
    cabinet.code = cast(str | None, values.get("code"))
    cabinet.region_id = (
        coerce_uuid(region_id) if (region_id := cast(str | None, values.get("region_id"))) else None
    )
    cabinet.latitude = latitude
    cabinet.longitude = longitude
    cabinet.notes = cast(str | None, values.get("notes"))
    cabinet.is_active = bool(values.get("is_active"))


def commit_cabinet_update(db: Session, cabinet: FdhCabinet, values: dict[str, object]) -> None:
    """Apply form values and flush the cabinet update."""
    update_cabinet(cabinet, values)
    db.flush()


def update_cabinet_submission(
    db: Session,
    cabinet: FdhCabinet,
    form: FormData,
    *,
    action_url: str,
) -> dict[str, object]:
    """Handle FDH cabinet update form parsing/validation/update."""
    before_snapshot = model_to_dict(cabinet)
    values = parse_form_values(form)
    error = validate_name(str(values["name"]))
    if error:
        return {
            "error": error,
            "form_context": build_form_context(
                db,
                cabinet=cabinet,
                action_url=action_url,
                error=error,
            ),
        }
    commit_cabinet_update(db, cabinet, values)
    after_snapshot = model_to_dict(cabinet)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    return {"error": None, "form_context": None, "metadata": metadata}


def detail_page_data(db: Session, cabinet_id: str) -> dict[str, object] | None:
    """Return cabinet detail payload including splitters."""
    cabinet = get_cabinet(db, cabinet_id)
    if not cabinet:
        return None
    splitters = db.scalars(
        select(Splitter).where(Splitter.fdh_id == cabinet.id).order_by(Splitter.name)
    ).all()
    return {"cabinet": cabinet, "splitters": splitters}


def list_splitters_page_data(db: Session) -> dict[str, object]:
    """Return splitter list and summary stats."""
    splitters = db.scalars(
        select(Splitter)
        .where(Splitter.is_active.is_(True))
        .order_by(Splitter.name)
        .limit(200)
    ).all()
    return {"splitters": splitters, "stats": {"total": len(splitters)}}


def cabinets_for_splitter_forms(db: Session) -> list[FdhCabinet]:
    """Return active cabinets for splitter form select options."""
    return list(
        db.scalars(
            select(FdhCabinet).where(FdhCabinet.is_active.is_(True)).order_by(FdhCabinet.name)
        ).all()
    )


def get_splitter(db: Session, splitter_id: str) -> Splitter | None:
    """Get splitter by id."""
    return db.scalars(
        select(Splitter).where(Splitter.id == splitter_id)
    ).first()


def parse_splitter_form_values(form: FormData) -> dict[str, object]:
    """Parse splitter form fields into normalized values."""
    return {
        "name": _form_str(form, "name"),
        "fdh_id": (_form_str(form, "fdh_id") or None),
        "splitter_ratio": (_form_str(form, "splitter_ratio") or None),
        "notes": (_form_str(form, "notes") or None),
        "is_active": _form_str(form, "is_active") == "true",
        "input_ports_raw": _form_str(form, "input_ports"),
        "output_ports_raw": _form_str(form, "output_ports"),
    }


def validate_splitter_form(db: Session, values: dict[str, object]) -> str | None:
    """Validate required splitter form fields and FK references."""
    if not values.get("name"):
        return "Splitter name is required"
    fdh_id = values.get("fdh_id")
    if fdh_id:
        cabinet = db.scalars(
            select(FdhCabinet).where(FdhCabinet.id == fdh_id)
        ).first()
        if not cabinet:
            return "FDH cabinet not found"
    return None


def parse_splitter_port_counts(
    values: dict[str, object],
    *,
    default_input: int,
    default_output: int,
) -> tuple[int, int]:
    """Parse splitter port counts with fallbacks."""
    input_ports_raw = str(values.get("input_ports_raw") or "")
    output_ports_raw = str(values.get("output_ports_raw") or "")
    try:
        input_ports = int(input_ports_raw) if input_ports_raw else default_input
    except ValueError:
        input_ports = default_input
    try:
        output_ports = int(output_ports_raw) if output_ports_raw else default_output
    except ValueError:
        output_ports = default_output
    return input_ports, output_ports


def build_splitter_form_context(
    db: Session,
    *,
    splitter: Splitter | dict[str, object] | None,
    action_url: str,
    selected_fdh_id: str | None,
    error: str | None = None,
) -> dict[str, object]:
    """Build shared splitter form context."""
    context: dict[str, object] = {
        "splitter": splitter,
        "cabinets": cabinets_for_splitter_forms(db),
        "selected_fdh_id": selected_fdh_id,
        "action_url": action_url,
    }
    if error:
        context["error"] = error
    return context


def create_splitter(db: Session, values: dict[str, object]) -> Splitter:
    """Create and persist splitter from form values."""
    input_ports, output_ports = parse_splitter_port_counts(
        values,
        default_input=1,
        default_output=8,
    )
    splitter = Splitter(
        name=values["name"],
        fdh_id=values.get("fdh_id"),
        splitter_ratio=values.get("splitter_ratio"),
        input_ports=input_ports,
        output_ports=output_ports,
        notes=values.get("notes"),
        is_active=bool(values.get("is_active")),
    )
    db.add(splitter)
    db.commit()
    db.refresh(splitter)
    return splitter


def create_splitter_submission(
    db: Session,
    form: FormData,
    *,
    action_url: str,
) -> dict[str, object]:
    """Handle splitter create form parsing/validation/create."""
    values = parse_splitter_form_values(form)
    error = validate_splitter_form(db, values)
    if error:
        return {
            "splitter": None,
            "error": error,
            "form_context": build_splitter_form_context(
                db,
                splitter=None,
                action_url=action_url,
                selected_fdh_id=str(values.get("fdh_id") or "") or None,
                error=error,
            ),
        }
    splitter = create_splitter(db, values)
    return {"splitter": splitter, "error": None, "form_context": None}


def update_splitter(splitter: Splitter, values: dict[str, object]) -> None:
    """Apply parsed form values to existing splitter."""
    input_ports, output_ports = parse_splitter_port_counts(
        values,
        default_input=splitter.input_ports,
        default_output=splitter.output_ports,
    )
    splitter.name = cast(str, values["name"])
    splitter.fdh_id = (
        coerce_uuid(fdh_id) if (fdh_id := cast(str | None, values.get("fdh_id"))) else None
    )
    splitter.splitter_ratio = cast(str | None, values.get("splitter_ratio"))
    splitter.input_ports = input_ports
    splitter.output_ports = output_ports
    splitter.notes = cast(str | None, values.get("notes"))
    splitter.is_active = bool(values.get("is_active"))


def commit_splitter_update(db: Session, splitter: Splitter, values: dict[str, object]) -> None:
    """Apply form values and flush the splitter update."""
    update_splitter(splitter, values)
    db.flush()


def update_splitter_submission(
    db: Session,
    splitter: Splitter,
    form: FormData,
    *,
    action_url: str,
) -> dict[str, object]:
    """Handle splitter update form parsing/validation/update."""
    before_snapshot = model_to_dict(splitter)
    values = parse_splitter_form_values(form)
    error = validate_splitter_form(db, values)
    if error:
        return {
            "error": error,
            "form_context": build_splitter_form_context(
                db,
                splitter=splitter,
                action_url=action_url,
                selected_fdh_id=str(values.get("fdh_id") or "") or None,
                error=error,
            ),
        }
    commit_splitter_update(db, splitter, values)
    after_snapshot = model_to_dict(splitter)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    return {"error": None, "form_context": None, "metadata": metadata}


def splitter_detail_page_data(db: Session, splitter_id: str) -> dict[str, object] | None:
    """Return splitter detail payload including ports."""
    from app.models.network import SplitterPort

    splitter = get_splitter(db, splitter_id)
    if not splitter:
        return None
    ports = db.scalars(
        select(SplitterPort)
        .where(SplitterPort.splitter_id == splitter.id)
        .order_by(SplitterPort.port_number)
    ).all()
    return {"splitter": splitter, "ports": ports}
