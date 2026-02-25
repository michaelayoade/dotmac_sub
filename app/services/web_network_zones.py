"""Service helpers for admin network zone web routes."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.network import FdhCabinet, NetworkZone, OntUnit, Splitter
from app.services import network as network_service

logger = logging.getLogger(__name__)


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value.strip() if isinstance(value, str) else default


def list_page_data(db: Session, status: str | None = None) -> dict[str, object]:
    """Return zone list with summary stats."""
    status_filter = (status or "all").strip().lower()
    is_active: bool | None = None
    if status_filter == "active":
        is_active = True
    elif status_filter == "inactive":
        is_active = False

    zones = network_service.network_zones.list(db, is_active=is_active)

    # Compute per-zone stats
    zone_stats: dict[str, dict[str, int]] = {}
    for zone in zones:
        ont_count = (
            db.scalar(
                select(func.count())
                .select_from(OntUnit)
                .where(OntUnit.zone_id == zone.id)
            )
            or 0
        )
        splitter_count = (
            db.scalar(
                select(func.count())
                .select_from(Splitter)
                .where(Splitter.zone_id == zone.id)
            )
            or 0
        )
        fdh_count = (
            db.scalar(
                select(func.count())
                .select_from(FdhCabinet)
                .where(FdhCabinet.zone_id == zone.id)
            )
            or 0
        )
        zone_stats[str(zone.id)] = {
            "ont_count": ont_count,
            "splitter_count": splitter_count,
            "fdh_count": fdh_count,
        }

    total = len(zones)
    active = sum(1 for z in zones if z.is_active)

    return {
        "zones": zones,
        "zone_stats": zone_stats,
        "status_filter": status_filter,
        "stats": {
            "total": total,
            "active": active,
            "inactive": total - active,
        },
    }


def detail_page_data(db: Session, zone_id: str) -> dict[str, object] | None:
    """Return zone detail page payload."""
    zone = network_service.network_zones.get_or_none(db, zone_id)
    if not zone:
        return None

    # Child zones
    children = network_service.network_zones.list(
        db, parent_id=str(zone.id), is_active=True
    )

    # Parent chain
    parent = None
    if zone.parent_id:
        parent = network_service.network_zones.get_or_none(db, str(zone.parent_id))

    # Associated infrastructure counts
    ont_count = (
        db.scalar(
            select(func.count())
            .select_from(OntUnit)
            .where(OntUnit.zone_id == zone.id)
        )
        or 0
    )
    splitter_count = (
        db.scalar(
            select(func.count())
            .select_from(Splitter)
            .where(Splitter.zone_id == zone.id)
        )
        or 0
    )
    fdh_count = (
        db.scalar(
            select(func.count())
            .select_from(FdhCabinet)
            .where(FdhCabinet.zone_id == zone.id)
        )
        or 0
    )

    return {
        "zone": zone,
        "parent": parent,
        "children": children,
        "infra_stats": {
            "ont_count": ont_count,
            "splitter_count": splitter_count,
            "fdh_count": fdh_count,
        },
    }


def build_form_context(
    db: Session,
    *,
    zone: NetworkZone | None,
    action_url: str,
    error: str | None = None,
) -> dict[str, object]:
    """Build form context for create/edit."""
    # Available parent zones (exclude self for edit)
    parent_zones = network_service.network_zones.list(db, is_active=True)
    if zone:
        parent_zones = [z for z in parent_zones if z.id != zone.id]

    context: dict[str, object] = {
        "zone": zone,
        "parent_zones": parent_zones,
        "action_url": action_url,
    }
    if error:
        context["error"] = error
    return context


def parse_form_values(form: FormData) -> dict[str, object]:
    """Parse zone form fields into normalized values."""
    lat_str = _form_str(form, "latitude")
    lon_str = _form_str(form, "longitude")
    latitude = float(lat_str) if lat_str else None
    longitude = float(lon_str) if lon_str else None

    return {
        "name": _form_str(form, "name"),
        "description": _form_str(form, "description") or None,
        "parent_id": _form_str(form, "parent_id") or None,
        "latitude": latitude,
        "longitude": longitude,
        "is_active": _form_str(form, "is_active") == "true",
    }


def validate_form(values: dict[str, object]) -> str | None:
    """Validate zone form values. Returns error message or None."""
    name = values.get("name")
    if not name or not str(name).strip():
        return "Zone name is required."
    return None


def create_zone(db: Session, values: dict[str, object]) -> NetworkZone:
    """Create a zone from validated form values."""
    return network_service.network_zones.create(
        db,
        name=str(values["name"]),
        description=str(values["description"]) if values.get("description") else None,
        parent_id=str(values["parent_id"]) if values.get("parent_id") else None,
        latitude=values.get("latitude"),  # type: ignore[arg-type]
        longitude=values.get("longitude"),  # type: ignore[arg-type]
        is_active=bool(values.get("is_active", True)),
    )


def update_zone(db: Session, zone_id: str, values: dict[str, object]) -> NetworkZone:
    """Update a zone from validated form values."""
    parent_id = values.get("parent_id")
    return network_service.network_zones.update(
        db,
        zone_id,
        name=str(values["name"]),
        description=str(values["description"]) if values.get("description") else None,
        parent_id=str(parent_id) if parent_id else None,
        clear_parent=not parent_id,
        latitude=values.get("latitude"),  # type: ignore[arg-type]
        longitude=values.get("longitude"),  # type: ignore[arg-type]
        is_active=bool(values.get("is_active", True)),
    )


def zones_for_forms(db: Session) -> list[NetworkZone]:
    """Return active zones for form select dropdowns."""
    return network_service.network_zones.list(db, is_active=True)
