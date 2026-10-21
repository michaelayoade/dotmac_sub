"""Canonical JSON, JSON pointers and layer-document directives."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

DELETE = "$delete"
ADD = "$add"
REMOVE = "$remove"
REPLACE = "$replace"
DIRECTIVES = frozenset({DELETE, ADD, REMOVE, REPLACE})


class DocumentError(ValueError):
    """A layer or exception document is malformed (rejected on write)."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path or '/'}: {message}")
        self.path = path
        self.message = message


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_plain(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True)
        )
    return value


def canonical_json(value: Any) -> str:
    """Sorted keys, list order kept, UTF-8, no insignificant whitespace."""
    return json.dumps(
        _plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_of(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def escape_segment(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def pointer(parts: Sequence[str]) -> str:
    return "".join("/" + escape_segment(part) for part in parts)


def element_segment(canonical_element: str) -> str:
    """Union elements are addressed by percent-encoded canonical text."""
    return quote(canonical_element, safe="")


def is_directive_dict(value: Any) -> bool:
    return isinstance(value, Mapping) and any(
        isinstance(k, str) and k.startswith("$") for k in value
    )


def is_delete(value: Any) -> bool:
    if not isinstance(value, Mapping) or DELETE not in value:
        return False
    if value[DELETE] is not True or len(value) != 1:
        raise DocumentError("", '$delete must be exactly {"$delete": true}')
    return True


def check_directive_keys(
    value: Mapping[str, Any], allowed: frozenset[str], path: str
) -> None:
    for key in value:
        if isinstance(key, str) and key.startswith("$") and key not in allowed:
            raise DocumentError(path, f"directive {key} is not valid here")


@dataclass(frozen=True)
class UnionPatch:
    add: tuple[Any, ...] = ()
    remove: tuple[Any, ...] = ()
    replace: tuple[Any, ...] | None = None


def parse_union_patch(value: Any, path: str) -> UnionPatch:
    if isinstance(value, (list, tuple)):
        return UnionPatch(add=tuple(value))
    if isinstance(value, Mapping):
        check_directive_keys(value, frozenset({ADD, REMOVE, REPLACE}), path)
        if not value or any(not str(k).startswith("$") for k in value):
            raise DocumentError(path, "a union patch holds only $add/$remove/$replace")
        if REPLACE in value and (ADD in value or REMOVE in value):
            raise DocumentError(path, "$replace cannot be combined with $add/$remove")
        for key in value:
            if not isinstance(value[key], (list, tuple)):
                raise DocumentError(path, f"{key} needs a list")
        replace = tuple(value[REPLACE]) if REPLACE in value else None
        return UnionPatch(
            add=tuple(value.get(ADD, ())),
            remove=tuple(value.get(REMOVE, ())),
            replace=replace,
        )
    raise DocumentError(path, "a union field takes a list or a directive object")
