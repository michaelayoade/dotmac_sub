"""ONT serial normalization and matching helpers."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.services.network.serial_utils import search_candidates


def normalize_ont_serial(serial: str | None) -> str:
    """Normalize ONT serial for comparison: uppercase, strip non-alphanumeric."""
    if not serial:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", serial).upper()


def normalized_serial_candidates(serial: str | None) -> set[str]:
    """Return normalized serial variants for cross-system matching."""
    return {
        normalized
        for candidate in search_candidates(serial)
        if (normalized := normalize_ont_serial(candidate))
    }


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
    if normalized.startswith("UNKNOWN"):
        return True
    if text.upper().startswith("AUTO-") or normalized in {"AUTO", "AUTOGEN"}:
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


def find_unique_active_ont_by_serial(
    db: Session,
    serial: str | None,
    *,
    exclude_ont_id: object | None = None,
) -> OntUnit | None:
    """Find exactly one active ONT matching any canonical serial variant.

    Ambiguous duplicate serials return ``None`` so callers do not link ACS or
    OLT observations to the wrong physical ONT.  This is intentionally stricter
    than a ``first()`` lookup until the database can enforce global normalized
    serial uniqueness.
    """
    candidates = normalized_serial_candidates(serial)
    if not candidates:
        return None

    rows = list(db.scalars(select(OntUnit).where(OntUnit.is_active.is_(True))).all())
    matches: list[OntUnit] = []
    seen_ids: set[object] = set()
    for ont in rows:
        if exclude_ont_id is not None and ont.id == exclude_ont_id:
            continue
        serial_values = [
            getattr(ont, "serial_number", None),
            getattr(ont, "vendor_serial_number", None),
        ]
        for value in serial_values:
            if not value or looks_synthetic_ont_serial(value):
                continue
            if normalized_serial_candidates(str(value)) & candidates:
                if ont.id not in seen_ids:
                    matches.append(ont)
                    seen_ids.add(ont.id)
                break

    return matches[0] if len(matches) == 1 else None
