"""Field specifications, canonicalisers and schema introspection."""

from __future__ import annotations

import ipaddress
import re
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel


class Kind(StrEnum):
    object = "object"
    map = "map"
    scalar = "scalar"
    list = "list"


class Strategy(Enum):
    replace = "replace"
    union = "union"
    keyed = "keyed"


class AccessKind(StrEnum):
    neutral = "neutral"
    allow_list = "allow_list"
    deny_list = "deny_list"
    secure_bool = "secure_bool"
    ordered = "ordered"


class Tie(StrEnum):
    error = "error"
    restrictive = "restrictive"


class _Sentinel:
    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


REQUIRED: Any = _Sentinel("REQUIRED")
OPTIONAL_UNMANAGED: Any = _Sentinel("OPTIONAL_UNMANAGED")

# ----------------------------------------------------------------- canonicalisers

_HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)


def canon_cidr(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("a CIDR string is required")
    return str(ipaddress.ip_network(value.strip(), strict=False))


def canon_ip(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("an IP address string is required")
    return str(ipaddress.ip_address(value.strip()))


def canon_host(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("a host string is required")
    text = value.strip()
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    text = text.rstrip(".").lower()
    if not _HOST_RE.match(text):
        raise ValueError("not an IP address or host name")
    return text


def canon_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("a string is required")
    return value.strip()


def canon_lower(value: object) -> str:
    return canon_text(value).lower()


def canon_version(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d+(\.\d+)*", value.strip()):
        raise ValueError("a dotted version is required")
    return ".".join(str(int(part)) for part in value.strip().split("."))


def canon_none(value: object) -> object:
    return value


CANONICALISERS: Mapping[str, Callable[[Any], Any]] = {
    "none": canon_none,
    "cidr": canon_cidr,
    "ip": canon_ip,
    "host": canon_host,
    "text": canon_text,
    "lower": canon_lower,
    "version": canon_version,
}


@dataclass(frozen=True)
class FieldSpec:
    """Merge, access and default semantics of one schema field (§1.5)."""

    kind: Kind
    strategy: Strategy | None = None
    key: str | None = None
    order: str | None = None
    access: AccessKind = AccessKind.neutral
    secure: bool | None = None
    ranking: tuple[Any, ...] = ()
    tie: Tie = Tie.error
    derived: bool = False
    discoverable: bool = False
    secret: bool = False
    nullable: bool = False
    default: Any = REQUIRED
    canon: str = "none"

    def __post_init__(self) -> None:
        if self.canon not in CANONICALISERS:
            raise ValueError(f"unknown canonicaliser {self.canon!r}")
        if self.kind is Kind.list:
            if self.strategy is None:
                raise ValueError("a list field must declare a strategy")
            if self.strategy is Strategy.keyed and not self.key:
                raise ValueError("a keyed list must name its key field")
        elif self.strategy is not None:
            raise ValueError("only list fields declare a strategy")
        if self.access is AccessKind.secure_bool and not isinstance(self.secure, bool):
            raise ValueError("secure_bool needs the secure value")
        if self.access is AccessKind.ordered and not self.ranking:
            raise ValueError("ordered needs a ranking, most secure first")
        if (
            self.access in {AccessKind.allow_list, AccessKind.deny_list}
            and self.kind is not Kind.list
        ):
            raise ValueError("allow_list/deny_list apply to list fields")
        if self.access is AccessKind.secure_bool and self.kind is not Kind.scalar:
            raise ValueError("secure_bool applies to scalar fields")
        if self.tie is Tie.restrictive:
            if self.access is AccessKind.neutral:
                raise ValueError("tie=restrictive needs non-neutral access")
            if self.kind is Kind.list and self.strategy is not Strategy.union:
                raise ValueError("tie=restrictive on lists needs the union strategy")
        if self.derived and self.default is not OPTIONAL_UNMANAGED:
            raise ValueError("a derived field must be optional_unmanaged")

    @property
    def has_value_default(self) -> bool:
        return self.default is not REQUIRED and self.default is not OPTIONAL_UNMANAGED

    def canonicalise(self, value: Any) -> Any:
        return CANONICALISERS[self.canon](value)

    def security_rank(self, value: Any) -> int:
        """Lower is more secure. Only meaningful for secure_bool and ordered."""
        if self.access is AccessKind.secure_bool:
            return 0 if value == self.secure else 1
        if self.access is AccessKind.ordered:
            try:
                return self.ranking.index(value)
            except ValueError:
                return len(self.ranking)
        raise ValueError("field has no security ordering")


def scalar(**kw: Any) -> FieldSpec:
    return FieldSpec(kind=Kind.scalar, **kw)


def obj(**kw: Any) -> FieldSpec:
    kw.setdefault("default", OPTIONAL_UNMANAGED)
    return FieldSpec(kind=Kind.object, **kw)


def mapping(**kw: Any) -> FieldSpec:
    kw.setdefault("default", OPTIONAL_UNMANAGED)
    return FieldSpec(kind=Kind.map, **kw)


def lst(strategy: Strategy, **kw: Any) -> FieldSpec:
    return FieldSpec(kind=Kind.list, strategy=strategy, **kw)


# --------------------------------------------------------------------- spec index

Path = tuple[str, ...]


def parse_spec_pointer(pointer: str) -> Path:
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise ValueError(f"spec pointer must start with '/': {pointer!r}")
    return tuple(
        part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")
    )


class SpecIndex:
    """JSON-pointer -> FieldSpec lookup with ``*`` wildcard segments."""

    def __init__(self, specs: Mapping[str, FieldSpec]) -> None:
        self._specs: dict[Path, FieldSpec] = {
            parse_spec_pointer(pointer): spec for pointer, spec in specs.items()
        }

    def __contains__(self, path: Path) -> bool:
        return path in self._specs

    def items(self) -> list[tuple[Path, FieldSpec]]:
        return sorted(self._specs.items(), key=lambda item: item[0])

    def get_exact(self, path: Path) -> FieldSpec | None:
        return self._specs.get(path)

    def lookup(self, path: Path) -> FieldSpec | None:
        resolved: Path = ()
        for segment in path:
            candidate = resolved + (segment,)
            resolved = candidate if candidate in self._specs else resolved + ("*",)
        return self._specs.get(resolved)

    def resolve(self, path: Path) -> Path:
        resolved: Path = ()
        for segment in path:
            candidate = resolved + (segment,)
            resolved = candidate if candidate in self._specs else resolved + ("*",)
        return resolved

    def children(self, path: Path) -> list[tuple[str, FieldSpec]]:
        depth = len(path)
        return sorted(
            (spec_path[depth], spec)
            for spec_path, spec in self._specs.items()
            if len(spec_path) == depth + 1 and spec_path[:depth] == path
        )

    def has_default_below(self, path: Path) -> bool:
        return any(
            spec_path[: len(path)] == path
            and len(spec_path) > len(path)
            and (spec.has_value_default or spec.default is REQUIRED)
            and spec.kind in {Kind.scalar, Kind.list}
            for spec_path, spec in self._specs.items()
        )


# ------------------------------------------------------------- schema introspection


@dataclass(frozen=True)
class SchemaLeaf:
    path: Path
    kind: Kind
    annotation: Any
    element: Any = None


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        nullable = len(args) != len(get_args(annotation))
        if len(args) == 1:
            return args[0], nullable
        return annotation, nullable
    return annotation, False


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def walk_schema(model: type[BaseModel], prefix: Path = ()) -> list[SchemaLeaf]:
    """Every field of a pydantic parameter model as a pointer path plus kind."""
    leaves: list[SchemaLeaf] = []
    for name, info in model.model_fields.items():
        annotation, _nullable = _unwrap_optional(info.annotation)
        path = prefix + (name,)
        origin = get_origin(annotation)
        if _is_model(annotation):
            leaves.append(SchemaLeaf(path, Kind.object, annotation))
            leaves.extend(walk_schema(annotation, path))
        elif origin is dict:
            value_type = get_args(annotation)[1]
            leaves.append(SchemaLeaf(path, Kind.map, annotation))
            if _is_model(value_type):
                leaves.extend(walk_schema(value_type, path + ("*",)))
            else:
                leaves.append(SchemaLeaf(path + ("*",), Kind.scalar, value_type))
        elif origin is list:
            element = get_args(annotation)[0]
            leaves.append(SchemaLeaf(path, Kind.list, annotation, element))
            if _is_model(element):
                leaves.extend(walk_schema(element, path + ("*",)))
        else:
            leaves.append(SchemaLeaf(path, Kind.scalar, annotation))
    return leaves


def schema_problems(
    model: type[BaseModel], specs: Mapping[str, FieldSpec]
) -> list[str]:
    """Guard 10: every schema leaf has a complete spec and every spec a leaf."""
    index = SpecIndex(specs)
    problems: list[str] = []
    leaves = {leaf.path: leaf for leaf in walk_schema(model)}
    for path, leaf in sorted(leaves.items()):
        spec = index.get_exact(path)
        pointer = "/" + "/".join(path)
        if spec is None:
            problems.append(f"{pointer}: no FieldSpec")
            continue
        if spec.kind is not leaf.kind:
            problems.append(
                f"{pointer}: spec kind {spec.kind} != schema kind {leaf.kind}"
            )
        if leaf.kind is Kind.list and spec.strategy is None:
            problems.append(f"{pointer}: list without strategy")
    known = {spec_path for spec_path, _ in index.items()}
    for spec_path in sorted(known - set(leaves)):
        problems.append(
            f"/{'/'.join(spec_path)}: spec for a field the schema does not have"
        )
    return problems
