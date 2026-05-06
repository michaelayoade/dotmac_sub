"""Helpers for ONT equipment identity values."""

from __future__ import annotations

import re


_OLT_CHASSIS_MODEL_RE = re.compile(
    r"^(?:MA56(?:00|08T)|MA58(?:00|08)(?:-[A-Z0-9]+)?|MA5600V[A-Z0-9]+|MA5800V[A-Z0-9]+)$",
    re.IGNORECASE,
)


def normalize_ont_equipment_id(value: object | None) -> str | None:
    """Return a usable ONT equipment ID, rejecting OLT chassis identifiers."""
    equipment_id = str(value or "").strip()
    if not equipment_id:
        return None
    if _OLT_CHASSIS_MODEL_RE.fullmatch(equipment_id):
        return None
    return equipment_id
