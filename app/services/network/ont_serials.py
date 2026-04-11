"""ONT serial normalization and matching helpers."""

from __future__ import annotations

import re

from app.models.network import OntUnit


def normalize_ont_serial(serial: str | None) -> str:
    """Normalize ONT serial for comparison: uppercase, strip non-alphanumeric."""
    if not serial:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", serial).upper()


def prefer_ont_candidate(
    existing: OntUnit | None,
    new_candidate: OntUnit,
    *,
    active_assignment_ont_ids: set | None = None,
) -> OntUnit:
    """Select the preferred ONT when duplicates exist."""
    if existing is None:
        return new_candidate
    active_ids = active_assignment_ont_ids or set()

    existing_has_assignment = existing.id in active_ids
    new_has_assignment = new_candidate.id in active_ids
    if new_has_assignment and not existing_has_assignment:
        return new_candidate
    if existing_has_assignment and not new_has_assignment:
        return existing

    if new_candidate.is_active and not existing.is_active:
        return new_candidate
    if existing.is_active and not new_candidate.is_active:
        return existing

    existing_updated = getattr(existing, "updated_at", None)
    new_updated = getattr(new_candidate, "updated_at", None)
    if new_updated and existing_updated and new_updated > existing_updated:
        return new_candidate
    return existing


def looks_synthetic_ont_serial(serial: str | None) -> bool:
    """Return True if serial looks auto-generated or placeholder."""
    if not serial:
        return True
    text = str(serial or "").strip()
    if re.match(
        r"^(HW|ZT|NK|OLT)-[A-F0-9]{8}-[A-Z0-9]+(?:-\d{10,20})?$",
        text,
        re.IGNORECASE,
    ):
        return True
    normalized = normalize_ont_serial(serial)
    if len(normalized) < 4:
        return True
    if normalized in ("0000000000000000", "FFFFFFFFFFFFFFFF"):
        return True
    if normalized.startswith("UNKNOWN") or normalized.startswith("AUTO"):
        return True
    return False


def is_plausible_vendor_serial(vendor_serial: str | None) -> bool:
    """Return True if vendor serial looks like a real device serial."""
    if not vendor_serial:
        return False
    normalized = normalize_ont_serial(vendor_serial)
    if len(normalized) < 8 or len(normalized) > 24:
        return False
    if looks_synthetic_ont_serial(vendor_serial):
        return False
    return True
