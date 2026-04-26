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

_CONFIG_PACK_OWNED_PATHS: tuple[tuple[str, ...], ...] = (
    ("tr069",),
    ("authorization",),
    ("omci",),
    ("wan", "vlan"),
    ("wan", "gem_index"),
    ("management", "vlan"),
)


def _is_config_pack_owned_path(path: tuple[str, ...]) -> bool:
    return any(path[: len(owned)] == owned for owned in _CONFIG_PACK_OWNED_PATHS)


def strip_config_pack_owned_desired_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return desired config without fields owned by the OLT config pack."""
    cleaned = deepcopy(config)

    for path in _CONFIG_PACK_OWNED_PATHS:
        cursor: Any = cleaned
        parents: list[tuple[dict[str, Any], str]] = []
        for part in path[:-1]:
            if not isinstance(cursor, dict):
                break
            parents.append((cursor, part))
            cursor = cursor.get(part)
        else:
            if isinstance(cursor, dict):
                cursor.pop(path[-1], None)
                for parent, key in reversed(parents):
                    child = parent.get(key)
                    if isinstance(child, dict) and not child:
                        parent.pop(key, None)
                    else:
                        break

    return cleaned


def desired_config(ont: OntUnit) -> dict[str, Any]:
    """Return a mutable desired-config dict for an ONT."""
    current = getattr(ont, "desired_config", None)
    if isinstance(current, dict):
        return strip_config_pack_owned_desired_config(current)
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
    if _is_config_pack_owned_path(path):
        ont.desired_config = strip_config_pack_owned_desired_config(desired_config(ont))
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
