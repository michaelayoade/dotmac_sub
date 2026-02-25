"""Service helpers for admin network speed profile web routes."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.requests import Request

from app.models.network import SpeedProfileDirection, SpeedProfileType
from app.services.network.speed_profiles import format_speed, speed_profiles

logger = logging.getLogger(__name__)


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value.strip() if isinstance(value, str) else default


def list_context(
    request: Request,
    db: Session,
    *,
    direction: str | None = None,
    search: str | None = None,
) -> dict[str, object]:
    """Build context for the speed profiles list page."""
    from app.web.admin import get_current_user, get_sidebar_stats

    active_tab = direction if direction in ("download", "upload") else "download"

    download_profiles = speed_profiles.list(
        db,
        direction="download",
        is_active=True,
        search=search,
    )
    upload_profiles = speed_profiles.list(
        db,
        direction="upload",
        is_active=True,
        search=search,
    )

    profile_counts = speed_profiles.count_by_profile(db)

    # Attach formatted speed to each profile for template convenience
    for p in download_profiles:
        p._speed_display = format_speed(p.speed_kbps)  # type: ignore[attr-defined]
    for p in upload_profiles:
        p._speed_display = format_speed(p.speed_kbps)  # type: ignore[attr-defined]

    return {
        "request": request,
        "active_page": "speed-profiles",
        "active_menu": "network",
        "active_tab": active_tab,
        "download_profiles": download_profiles,
        "upload_profiles": upload_profiles,
        "profile_counts": profile_counts,
        "search": search or "",
        "format_speed": format_speed,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def form_context(
    request: Request,
    db: Session,
    *,
    profile_id: str | None = None,
    direction: str | None = None,
    error: str | None = None,
    form_values: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build context for the speed profile create/edit form."""
    from app.web.admin import get_current_user, get_sidebar_stats

    profile = None
    if profile_id:
        profile = speed_profiles.get(db, profile_id)

    directions = [d.value for d in SpeedProfileDirection]
    speed_types = [t.value for t in SpeedProfileType]

    context: dict[str, object] = {
        "request": request,
        "active_page": "speed-profiles",
        "active_menu": "network",
        "profile": profile,
        "directions": directions,
        "speed_types": speed_types,
        "format_speed": format_speed,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }

    if profile_id and profile:
        context["action_url"] = f"/admin/network/speed-profiles/{profile_id}/edit"
    else:
        context["action_url"] = "/admin/network/speed-profiles/create"

    # Pre-fill direction from query param when creating
    if not profile and direction:
        context["default_direction"] = direction
    else:
        context["default_direction"] = None

    if form_values:
        context["form_values"] = form_values

    if error:
        context["error"] = error

    return context


def parse_form_values(form: FormData) -> dict[str, object]:
    """Parse speed profile form fields into normalized values."""
    speed_raw = _form_str(form, "speed_kbps")
    try:
        speed_kbps = int(speed_raw) if speed_raw else 0
    except ValueError:
        speed_kbps = 0

    return {
        "name": _form_str(form, "name"),
        "direction": _form_str(form, "direction"),
        "speed_kbps": speed_kbps,
        "speed_type": _form_str(form, "speed_type") or "internet",
        "use_prefix_suffix": _form_str(form, "use_prefix_suffix") == "true",
        "is_default": _form_str(form, "is_default") == "true",
        "notes": _form_str(form, "notes") or None,
    }


def validate_form(values: dict[str, object]) -> str | None:
    """Validate speed profile form values. Returns error message or None."""
    name = values.get("name")
    if not name or not str(name).strip():
        return "Profile name is required."

    direction = values.get("direction")
    if not direction or str(direction) not in ("download", "upload"):
        return "Direction must be 'download' or 'upload'."

    speed_kbps = values.get("speed_kbps")
    if not speed_kbps or int(str(speed_kbps)) <= 0:
        return "Speed must be a positive integer (in Kbps)."

    speed_type = values.get("speed_type")
    if speed_type and str(speed_type) not in ("internet", "management"):
        return "Invalid speed type."

    return None


def handle_create(db: Session, form_data: dict[str, object]) -> SpeedProfileDirection:
    """Create a speed profile from validated form values. Returns the direction."""
    direction_value = str(form_data["direction"])
    direction_enum = SpeedProfileDirection(direction_value)
    speed_type_enum = SpeedProfileType(str(form_data.get("speed_type", "internet")))

    speed_profiles.create(
        db,
        name=str(form_data["name"]),
        direction=direction_enum,
        speed_kbps=int(str(form_data["speed_kbps"])),
        speed_type=speed_type_enum,
        use_prefix_suffix=bool(form_data.get("use_prefix_suffix", False)),
        is_default=bool(form_data.get("is_default", False)),
        notes=str(form_data["notes"]) if form_data.get("notes") else None,
    )
    return direction_enum


def handle_update(
    db: Session,
    profile_id: str,
    form_data: dict[str, object],
) -> SpeedProfileDirection:
    """Update a speed profile from validated form values. Returns the direction."""
    direction_value = str(form_data["direction"])
    direction_enum = SpeedProfileDirection(direction_value)
    speed_type_enum = SpeedProfileType(str(form_data.get("speed_type", "internet")))

    speed_profiles.update(
        db,
        profile_id,
        name=str(form_data["name"]),
        direction=direction_enum,
        speed_kbps=int(str(form_data["speed_kbps"])),
        speed_type=speed_type_enum,
        use_prefix_suffix=bool(form_data.get("use_prefix_suffix", False)),
        is_default=bool(form_data.get("is_default", False)),
        notes=str(form_data["notes"]) if form_data.get("notes") else None,
    )
    return direction_enum
