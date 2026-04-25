"""Helpers for enforcing bundle + sparse override semantics on ONTs."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntConfigOverride, OntConfigOverrideSource, OntUnit
from app.services.network.ont_bundle_assignments import get_active_bundle_assignment


def _normalize_override_value(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    return str(value)


def is_bundle_managed_ont(db: Session, ont: OntUnit) -> bool:
    """Return True when an ONT should use bundle + overrides as source of truth."""
    return get_active_bundle_assignment(db, ont) is not None


def upsert_ont_config_override(
    db: Session,
    *,
    ont: OntUnit,
    field_name: str,
    value: Any,
    source: OntConfigOverrideSource = OntConfigOverrideSource.operator,
    reason: str | None = None,
) -> None:
    """Create, update, or delete one explicit ONT config override row."""
    row = db.scalars(
        select(OntConfigOverride)
        .where(OntConfigOverride.ont_unit_id == ont.id)
        .where(OntConfigOverride.field_name == field_name)
        .limit(1)
    ).first()

    normalized = _normalize_override_value(value)
    if normalized is None:
        if row is not None:
            db.delete(row)
        return

    if row is None:
        row = OntConfigOverride(
            ont_unit_id=ont.id,
            field_name=field_name,
            source=source,
        )
        db.add(row)
    else:
        row.source = source
    row.value_json = {"value": normalized}
    row.reason = reason
