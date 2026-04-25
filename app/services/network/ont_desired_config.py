"""Helpers for ONT-local desired configuration intent.

Desired ONT config is stored directly on ``OntUnit.desired_config``. OLT/site
defaults remain in ``OltConfigPack`` and are merged at read time by
``effective_ont_config``.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.models.network import OntUnit


# Legacy field mappings are no longer needed. All callers now use canonical
# dotted paths (e.g., "wan.mode", "management.vlan") which resolve_field_path()
# handles via the default split(".") fallback.
_LEGACY_FIELD_PATHS: dict[str, tuple[str, ...]] = {}


def desired_config(ont: OntUnit) -> dict[str, Any]:
    """Return a mutable desired-config dict for an ONT."""
    current = getattr(ont, "desired_config", None)
    if isinstance(current, dict):
        return deepcopy(current)
    return {}


def resolve_field_path(field_name: str) -> tuple[str, ...]:
    """Map old flat/override field names to desired_config paths."""
    if field_name in _LEGACY_FIELD_PATHS:
        return _LEGACY_FIELD_PATHS[field_name]
    return tuple(part for part in str(field_name).split(".") if part)


def _normalize_value(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, dict) and "value" in value:
        return _normalize_value(value.get("value"))
    return value


def set_desired_config_value(
    ont: OntUnit,
    field_name: str,
    value: Any,
) -> None:
    """Set or clear one desired-config value on an ONT."""
    path = resolve_field_path(field_name)
    if not path:
        return

    config = desired_config(ont)
    normalized = _normalize_value(value)
    if normalized is None:
        cursor = config
        for part in path[:-1]:
            next_value = cursor.get(part)
            if not isinstance(next_value, dict):
                ont.desired_config = config
                return
            cursor = next_value
        cursor.pop(path[-1], None)
        ont.desired_config = config
        return

    cursor = config
    for part in path[:-1]:
        next_value = cursor.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[part] = next_value
        cursor = next_value
    cursor[path[-1]] = normalized
    ont.desired_config = config


def upsert_ont_desired_config_value(
    db: object,
    *,
    ont: OntUnit,
    field_name: str,
    value: Any,
    **_: Any,
) -> None:
    """Compatibility-shaped setter for services that persist ONT intent."""
    set_desired_config_value(ont, field_name, value)


def get_desired_config_value(
    config: dict[str, Any],
    *path: str,
    default: Any = None,
) -> Any:
    """Read one nested desired-config value."""
    cursor: Any = config
    for part in path:
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor
