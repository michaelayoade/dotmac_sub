"""Deterministic merge with provenance and equal-rank conflicts (§1.5, M1-M11)."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .document import (
    DELETE,
    REPLACE,
    DocumentError,
    canonical_json,
    check_directive_keys,
    element_segment,
    is_delete,
    parse_union_patch,
    pointer,
)
from .fields import AccessKind, FieldSpec, Kind, Path, SpecIndex, Strategy, Tie

MODE_POINTER = "/$mode"
DELETED_VALUE = "$delete"


class MergeError(DocumentError):
    """A patch cannot be merged (rejected on write)."""


@dataclass(frozen=True)
class Origin:
    """The layer (or exception, default, derivation) that produced a value."""

    tier: str
    rank: int
    layer_id: str
    layer_name: str
    revision: int
    weight: int
    op: str = "set"
    at: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def group(self) -> tuple[int, int]:
        return (self.rank, self.weight)

    def with_op(self, op: str) -> Origin:
        return replace(self, op=op)

    def label(self) -> str:
        return f"{self.tier} {self.layer_name} r{self.revision}"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "tier": self.tier,
            "layer_id": self.layer_id,
            "layer": self.layer_name,
            "revision": self.revision,
            "weight": self.weight,
            "op": self.op,
        }
        if self.at:
            out["at"] = self.at
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


@dataclass(frozen=True)
class Shadow:
    origin: Origin
    value: Any


@dataclass(frozen=True)
class Provenance:
    origin: Origin
    value: Any = None
    shadowed: tuple[Shadow, ...] = ()
    contributors: tuple[Origin, ...] = ()


@dataclass(frozen=True)
class Tombstone:
    deleted_by: Origin
    shadowed: tuple[Shadow, ...] = ()


@dataclass(frozen=True)
class Conflict:
    path: str
    layers: tuple[str, ...]
    values: tuple[Any, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "layers": list(self.layers),
            "values": list(self.values),
        }


def _canon_value(spec: FieldSpec, value: Any) -> Any:
    try:
        return spec.canonicalise(value)
    except (ValueError, TypeError) as exc:
        raise MergeError("", f"invalid value {value!r}: {exc}") from exc


@dataclass(frozen=True)
class Write:
    """One layer's write to one path, kept per (path, rank, weight) group."""

    origin: Origin
    kind: str  # set | delete | replace | relative | merge
    value: Any = None
    add: tuple[Any, ...] = ()
    remove: tuple[Any, ...] = ()


def _ancestors(ptr: str) -> list[str]:
    """``ptr`` and its ancestors, nearest first, excluding the document root."""
    out: list[str] = []
    while ptr:
        out.append(ptr)
        ptr = ptr.rsplit("/", 1)[0]
    return out


def _stable(items: Sequence[Any], before: Sequence[Any]) -> list[Any]:
    """Name-independent order: elements of ``before`` first, then the rest sorted."""
    kept = [item for item in before if item in items]
    rest = sorted((item for item in items if item not in before), key=str)
    return kept + rest


class Merger:
    """Merges layer patches for one module into values plus provenance.

    Every write is logged per (path, rank, weight) so equal-rank outcomes never
    depend on layer names (M8, M10): restrictive fields resolve symmetrically,
    every other disagreement is a conflict that blocks planning.
    """

    def __init__(self, module_key: str, specs: SpecIndex) -> None:
        self.module_key = module_key
        self.specs = specs
        self.values: dict[str, Any] = {}
        self.meta: dict[str, Any] = {}
        self.prov: dict[str, Provenance] = {}
        self.tombs: dict[str, Tombstone] = {}
        self.conflicts: dict[str, Conflict] = {}
        self.lints: list[str] = []
        self._log: dict[tuple[str, tuple[int, int]], list[Write]] = {}
        self._before: dict[tuple[str, tuple[int, int]], list[Any]] = {}

    # ------------------------------------------------------------- public API

    def merge_patch(self, patch: Mapping[str, Any], origin: Origin) -> None:
        if not isinstance(patch, Mapping):
            raise MergeError("", "params must be an object")
        self._merge_object(self.values, patch, (), origin)

    def set_mode(self, mode: str, origin: Origin) -> None:
        spec = FieldSpec(Kind.scalar)
        self._assign(self.meta, "mode", MODE_POINTER, (), spec, mode, origin)

    @property
    def mode(self) -> str | None:
        value = self.meta.get("mode")
        return value if isinstance(value, str) else None

    def set_derived(self, path: Path, value: Any, origin: Origin) -> None:
        container: dict[str, Any] = self.values
        for segment in path[:-1]:
            container = container.setdefault(segment, {})
        container[path[-1]] = copy.deepcopy(value)
        ptr = pointer(path)
        self._note(ptr, origin, value)

    def origin_for(self, ptr: str) -> Origin | None:
        parts = ptr
        while True:
            hit = self.prov.get(parts)
            if hit:
                return hit.origin
            if not parts:
                return None
            parts = parts.rsplit("/", 1)[0]

    # -------------------------------------------------------------- write log

    def _others(self, ptr: str, origin: Origin) -> list[Write]:
        return [
            w
            for w in self._log.get((ptr, origin.group), [])
            if w.origin.layer_id != origin.layer_id
        ]

    def _record(self, ptr: str, write: Write) -> None:
        self._log.setdefault((ptr, write.origin.group), []).append(write)

    def _flag(self, ptr: str, writes: Sequence[Write]) -> None:
        """Record a conflict listing every layer that wrote ``ptr`` at this rank."""
        existing = self.conflicts.get(ptr)
        layers = list(existing.layers) if existing else []
        values = list(existing.values) if existing else []
        for write in writes:
            label = f"{write.origin.tier}:{write.origin.layer_name}"
            if label not in layers:
                layers.append(label)
                values.append(DELETED_VALUE if write.kind == "delete" else write.value)
        self.conflicts[ptr] = Conflict(ptr, tuple(layers), tuple(values))

    def _all_group_writes(self, ptr: str, origin: Origin) -> list[Write]:
        return list(self._log.get((ptr, origin.group), []))

    def _delete_clash(self, ptr: str, origin: Origin, own: Write) -> None:
        """A write at ``ptr`` meets an equal-rank delete of ``ptr`` or an ancestor."""
        for scope in _ancestors(ptr):
            deletes = [w for w in self._others(scope, origin) if w.kind == "delete"]
            if deletes:
                self._flag(scope, [*deletes, own])

    def _write_clash(self, ptr: str, origin: Origin, own: Write) -> None:
        """A delete at ``ptr`` meets equal-rank writes at or below ``ptr``."""
        prefix = ptr + "/"
        for (path, group), writes in sorted(self._log.items()):
            if group != origin.group or not (path == ptr or path.startswith(prefix)):
                continue
            hits = [
                w
                for w in writes
                if w.origin.layer_id != origin.layer_id and w.kind != "delete"
            ]
            if hits:
                self._flag(ptr, [*hits, own])

    # -------------------------------------------------------------- provenance

    def _note(
        self,
        ptr: str,
        origin: Origin,
        value: Any,
        *,
        keep_contributors: bool = False,
    ) -> None:
        current = self.prov.get(ptr)
        shadowed = current.shadowed if current else ()
        contributors: tuple[Origin, ...] = ()
        if current is not None:
            shadowed = shadowed + (Shadow(current.origin, current.value),)
            if keep_contributors:
                contributors = current.contributors
        self.prov[ptr] = Provenance(
            origin=origin,
            value=copy.deepcopy(value),
            shadowed=shadowed,
            contributors=contributors + (origin,),
        )
        self.tombs.pop(ptr, None)

    def _tombstone(self, path: Path, origin: Origin) -> None:
        ptr = pointer(path)
        prefix = ptr + "/"
        for key in sorted(k for k in self.prov if k == ptr or k.startswith(prefix)):
            current = self.prov.pop(key)
            self.tombs[key] = Tombstone(
                deleted_by=origin.with_op("delete"),
                shadowed=current.shadowed + (Shadow(current.origin, current.value),),
            )
        self.tombs.setdefault(ptr, Tombstone(deleted_by=origin.with_op("delete")))

    # ------------------------------------------------------------------ objects

    def _merge_object(
        self,
        base: dict[str, Any],
        patch: Mapping[str, Any],
        path: Path,
        origin: Origin,
    ) -> None:
        if not patch:
            return  # N4: an empty object is a no-op
        for key, value in patch.items():
            if not isinstance(key, str):
                raise MergeError(pointer(path), "keys must be strings")
            if key.startswith("$"):
                raise MergeError(
                    pointer(path + (key,)), f"directive {key} is not valid here"
                )
            child = path + (key,)
            ptr = pointer(child)
            spec = self.specs.lookup(child)
            if spec is None:
                raise MergeError(ptr, "no field spec for this path")
            if is_delete(value):
                self._delete(base, key, child, spec, origin)
                continue
            if spec.kind in {Kind.object, Kind.map}:
                if not isinstance(value, Mapping):
                    raise MergeError(ptr, "an object is required")
                if not value:
                    continue
                sub = base.get(key)
                if not isinstance(sub, dict):
                    sub = {}
                    base[key] = sub
                self._merge_object(sub, value, child, origin)
            elif spec.kind is Kind.scalar:
                if isinstance(value, (Mapping, list, tuple)):
                    raise MergeError(ptr, "a scalar is required")
                self._assign(base, key, ptr, child, spec, value, origin)
            else:
                assert spec.strategy is not None
                if spec.strategy is Strategy.replace:
                    self._replace_list(base, key, ptr, spec, value, origin)
                elif spec.strategy is Strategy.union:
                    self._union_list(base, key, ptr, spec, value, origin)
                else:
                    self._keyed_list(base, key, child, spec, value, origin)

    def _delete(
        self,
        base: dict[str, Any],
        key: str,
        path: Path,
        spec: FieldSpec,
        origin: Origin,
    ) -> None:
        ptr = pointer(path)
        own = Write(origin, "delete", DELETED_VALUE)
        self._write_clash(ptr, origin, own)
        self._record(ptr, own)
        if key in base:
            del base[key]
            self._tombstone(path, origin)
        else:
            self.lints.append(f"noop_delete {ptr}")
            self.tombs.setdefault(ptr, Tombstone(deleted_by=origin.with_op("delete")))

    # ------------------------------------------------------------------ scalars

    def _assign(
        self,
        base: dict[str, Any],
        key: str,
        ptr: str,
        path: Path,
        spec: FieldSpec,
        value: Any,
        origin: Origin,
    ) -> None:
        if value is None:
            if not spec.nullable:
                raise MergeError(ptr, "null is not allowed on a non-nullable field")
            new: Any = None
        else:
            new = _canon_value(spec, value)
        own = Write(origin, "set", new)
        self._delete_clash(ptr, origin, own)
        others = [w for w in self._others(ptr, origin) if w.kind == "set"]
        self._record(ptr, own)
        current = self.prov.get(ptr)
        if others and current is not None and current.origin.group == origin.group:
            old = base.get(key)
            if spec.tie is Tie.restrictive and spec.access in {
                AccessKind.secure_bool,
                AccessKind.ordered,
            }:
                if canonical_json(old) != canonical_json(new) and (
                    spec.security_rank(old) <= spec.security_rank(new)
                ):
                    # the incumbent is at least as secure: it stays
                    self.prov[ptr] = replace(
                        current,
                        contributors=current.contributors + (origin,),
                        shadowed=current.shadowed + (Shadow(origin, new),),
                    )
                    return
            elif any(canonical_json(w.value) != canonical_json(new) for w in others):
                self._flag(ptr, [*others, own])
        base[key] = new
        self._note(ptr, origin, new)

    # ------------------------------------------------------------- list writes

    def _replace_list(
        self,
        base: dict[str, Any],
        key: str,
        ptr: str,
        spec: FieldSpec,
        value: Any,
        origin: Origin,
    ) -> None:
        if isinstance(value, Mapping):
            check_directive_keys(value, frozenset(), ptr)
            raise MergeError(ptr, "a replace list takes a plain list")
        if not isinstance(value, (list, tuple)):
            raise MergeError(ptr, "a list is required")
        new = [_canon_value(spec, item) for item in value]
        self._list_op(base, key, ptr, spec, origin, replacement=new)

    def _union_list(
        self,
        base: dict[str, Any],
        key: str,
        ptr: str,
        spec: FieldSpec,
        value: Any,
        origin: Origin,
    ) -> None:
        patch = parse_union_patch(value, ptr)
        add = [_canon_value(spec, item) for item in patch.add]
        remove = [_canon_value(spec, item) for item in patch.remove]
        if set(map(str, add)) & set(map(str, remove)):
            raise MergeError(ptr, "the same element is in $add and $remove")
        if patch.replace is not None:
            replacement: list[Any] = []
            for item in patch.replace:
                canon = _canon_value(spec, item)
                if canon not in replacement:
                    replacement.append(canon)
            self._list_op(base, key, ptr, spec, origin, replacement=replacement)
            return
        self._list_op(base, key, ptr, spec, origin, add=add, remove=remove)

    def _list_op(
        self,
        base: dict[str, Any],
        key: str,
        ptr: str,
        spec: FieldSpec,
        origin: Origin,
        *,
        replacement: list[Any] | None = None,
        add: Sequence[Any] = (),
        remove: Sequence[Any] = (),
    ) -> None:
        """One layer's whole-list write ($replace / plain list) or $add/$remove."""
        group_key = (ptr, origin.group)
        kind = "replace" if replacement is not None else "relative"
        own = Write(
            origin,
            kind,
            copy.deepcopy(replacement)
            if replacement is not None
            else {"$add": list(add), "$remove": list(remove)},
            tuple(add),
            tuple(remove),
        )
        self._delete_clash(ptr, origin, own)
        others = [w for w in self._others(ptr, origin) if w.kind != "delete"]
        if group_key not in self._before:
            self._before[group_key] = copy.deepcopy(list(base.get(key, [])))
        self._record(ptr, own)
        restrictive = spec.tie is Tie.restrictive and spec.access in {
            AccessKind.allow_list,
            AccessKind.deny_list,
        }
        if others and restrictive:
            self._resolve_restrictive(base, key, ptr, spec, origin, own)
            return
        if others:
            self._flag_list_mix(ptr, spec, others, own)
        if replacement is not None:
            self._apply_replace(base, key, ptr, spec, replacement, origin)
        else:
            self._apply_relative(base, key, ptr, origin, add, remove)

    def _flag_list_mix(
        self, ptr: str, spec: FieldSpec, others: list[Write], own: Write
    ) -> None:
        """Non-restrictive equal-rank mixes: only add+add / remove+remove agree."""
        clashing: list[Write] = []
        for other in others:
            if own.kind == "replace" and other.kind == "replace":
                if canonical_json(own.value) != canonical_json(other.value):
                    clashing.append(other)
            elif own.kind != other.kind:
                clashing.append(other)  # replace against $add/$remove
            elif set(map(str, own.add)) & set(map(str, other.remove)) or set(
                map(str, own.remove)
            ) & set(map(str, other.add)):
                clashing.append(other)  # $add against $remove of one element
        if clashing:
            self._flag(ptr, [*others, own])

    def _apply_replace(
        self,
        base: dict[str, Any],
        key: str,
        ptr: str,
        spec: FieldSpec,
        new: list[Any],
        origin: Origin,
    ) -> None:
        base[key] = list(new)
        prefix = ptr + "/"
        for stale in [k for k in self.prov if k.startswith(prefix)]:
            del self.prov[stale]
        self._note(ptr, origin.with_op("set"), new)
        if spec.strategy is Strategy.union:
            for item in new:
                self.prov[ptr + "/" + element_segment(str(item))] = Provenance(
                    origin=origin.with_op("add"), value=item, contributors=(origin,)
                )

    def _apply_relative(
        self,
        base: dict[str, Any],
        key: str,
        ptr: str,
        origin: Origin,
        add: Sequence[Any],
        remove: Sequence[Any],
    ) -> None:
        current: list[Any] = list(base.get(key, []))
        for item in remove:
            if item in current:
                current.remove(item)
                self._tombstone_element(ptr, item, origin)
            else:
                self.lints.append(f"noop_remove {ptr}/{element_segment(str(item))}")
        for item in add:
            element_ptr = ptr + "/" + element_segment(str(item))
            if item not in current:
                current.append(item)
                self.tombs.pop(element_ptr, None)
                self.prov[element_ptr] = Provenance(
                    origin=origin.with_op("add"), value=item, contributors=(origin,)
                )
            else:
                seen = self.prov.get(element_ptr)
                if seen is not None:
                    self.prov[element_ptr] = replace(
                        seen, contributors=seen.contributors + (origin,)
                    )
        base[key] = current
        self._note(ptr, origin.with_op("merge"), current, keep_contributors=True)

    def _resolve_restrictive(
        self,
        base: dict[str, Any],
        key: str,
        ptr: str,
        spec: FieldSpec,
        origin: Origin,
        own: Write,
    ) -> None:
        """Equal-rank writes to a restrictive list, independent of layer names.

        allow_list: the intersection of every $replace list and of the
        relative result (before + all $add - all $remove); $remove beats $add.
        deny_list: the union of every $replace list and of the relative result
        (before - $remove not re-added + all $add); $add beats $remove.
        """
        writes = [
            w for w in self._log.get((ptr, origin.group), []) if w.kind != "delete"
        ]
        before = self._before[(ptr, origin.group)]
        replaces = [list(w.value) for w in writes if w.kind == "replace"]
        relative = [w for w in writes if w.kind == "relative"]
        adds = [item for w in relative for item in w.add]
        removes = [item for w in relative for item in w.remove]
        views: list[list[Any]] = []
        if relative:
            if spec.access is AccessKind.allow_list:
                view = [e for e in before if e not in removes]
                view += [e for e in adds if e not in view and e not in removes]
            else:
                view = [e for e in before if e not in removes or e in adds]
                view += [e for e in adds if e not in view]
            views.append(view)
        views.extend(replaces)
        if spec.access is AccessKind.allow_list:
            result = [e for e in views[0] if all(e in v for v in views[1:])]
        else:
            result = [e for v in views for e in v]
        unique: list[Any] = []
        for item in result:
            if item not in unique:
                unique.append(item)
        final = _stable(unique, before)
        previous = list(base.get(key, []))
        base[key] = final
        for item in previous:
            if item not in final:
                self._tombstone_element(ptr, item, origin)
        contributed = (
            list(own.add) + list(own.value or [])
            if own.kind == "replace"
            else list(own.add)
        )
        for item in final:
            element_ptr = ptr + "/" + element_segment(str(item))
            if item not in previous:
                self.tombs.pop(element_ptr, None)
                self.prov[element_ptr] = Provenance(
                    origin=origin.with_op("add"), value=item, contributors=(origin,)
                )
            elif item in contributed and element_ptr in self.prov:
                seen = self.prov[element_ptr]
                self.prov[element_ptr] = replace(
                    seen, contributors=seen.contributors + (origin,)
                )
        self._note(ptr, origin.with_op("merge"), final, keep_contributors=True)

    def _tombstone_element(self, ptr: str, item: Any, origin: Origin) -> None:
        element_ptr = ptr + "/" + element_segment(str(item))
        current = self.prov.pop(element_ptr, None)
        shadowed: tuple[Shadow, ...] = ()
        if current is not None:
            shadowed = current.shadowed + (Shadow(current.origin, current.value),)
        self.tombs[element_ptr] = Tombstone(origin.with_op("delete"), shadowed)

    # ------------------------------------------------------------- keyed lists

    def _keyed_list(
        self,
        base: dict[str, Any],
        key: str,
        path: Path,
        spec: FieldSpec,
        value: Any,
        origin: Origin,
    ) -> None:
        ptr = pointer(path)
        assert spec.key is not None
        key_spec = self.specs.lookup(path + ("*", spec.key))
        if key_spec is None:
            raise MergeError(ptr, f"no field spec for the key field {spec.key}")
        elements: dict[str, dict[str, Any]] = {}
        for existing in base.get(key, []):
            elements[str(existing[spec.key])] = existing
        if isinstance(value, Mapping):
            check_directive_keys(value, frozenset({REPLACE}), ptr)
            if set(value) != {REPLACE} or not isinstance(value[REPLACE], (list, tuple)):
                raise MergeError(ptr, 'a keyed patch is a list or {"$replace": [...]}')
            items = list(value[REPLACE])
            replace_all = True
        elif isinstance(value, (list, tuple)):
            items = list(value)
            replace_all = False
        else:
            raise MergeError(ptr, "a keyed field takes a list of objects")
        patch_keys: set[str] = set()
        parsed: list[tuple[str, Mapping[str, Any]]] = []
        for item in items:
            if not isinstance(item, Mapping) or spec.key not in item:
                raise MergeError(ptr, f"every element needs its key field {spec.key!r}")
            canon_key = str(_canon_value(key_spec, item[spec.key]))
            if canon_key in patch_keys:
                raise MergeError(ptr, f"duplicate key {canon_key!r} in one patch")
            patch_keys.add(canon_key)
            parsed.append((canon_key, item))
        keys_now = [canon for canon, _ in parsed]
        own = Write(origin, "replace" if replace_all else "merge", keys_now)
        self._delete_clash(ptr, origin, own)
        others = [w for w in self._others(ptr, origin) if w.kind != "delete"]
        self._record(ptr, own)
        if replace_all:
            for old_key in list(elements):
                self._tombstone(path + (old_key,), origin)
            elements = {}
        clashing = [
            w
            for w in others
            if (own.kind == "replace" or w.kind == "replace")
            and (
                own.kind != w.kind
                or canonical_json(sorted(w.value)) != canonical_json(sorted(keys_now))
            )
        ]
        if clashing:
            self._flag(ptr, [*others, own])
        for canon_key, item in parsed:
            element_path = path + (canon_key,)
            body = {k: v for k, v in item.items() if k != spec.key}
            if item.get(DELETE) is True and len(item) == 2:
                element_own = Write(origin, "delete", DELETED_VALUE)
                self._write_clash(pointer(element_path), origin, element_own)
                self._record(pointer(element_path), element_own)
                if canon_key in elements:
                    del elements[canon_key]
                    self._tombstone(element_path, origin)
                else:
                    self.lints.append(f"noop_delete {pointer(element_path)}")
                continue
            if any(isinstance(k, str) and k.startswith("$") for k in body):
                raise MergeError(
                    pointer(element_path),
                    "only {key, $delete: true} is a directive here",
                )
            element = elements.get(canon_key)
            if element is None:
                element = {spec.key: canon_key}
                elements[canon_key] = element
                element_ptr = pointer(element_path)
                self._delete_clash(element_ptr, origin, Write(origin, "set", canon_key))
                self._record(element_ptr, Write(origin, "set", canon_key))
                self._note(element_ptr, origin.with_op("add"), canon_key)
                self._note(
                    pointer(element_path + (spec.key,)),
                    origin.with_op("add"),
                    canon_key,
                )
                self.tombs.pop(element_ptr, None)
            self._merge_object(element, body, element_path, origin)
        ordered = list(elements.values())
        if spec.order:
            field_name = spec.order
            ordered.sort(
                key=lambda e: (e.get(field_name) is None, str(e.get(field_name, "")))
            )
        base[key] = ordered
        self._note(
            ptr,
            origin.with_op("merge"),
            [e[spec.key] for e in ordered],
            keep_contributors=True,
        )


# --------------------------------------------------------------- tree utilities


def leaf_pointers(specs: SpecIndex, values: Mapping[str, Any]) -> list[str]:
    """Pointers that must carry provenance: scalars, replace lists, union
    elements and keyed-element fields."""
    out: list[str] = []

    def walk(container: Mapping[str, Any], path: Path) -> None:
        for name, value in container.items():
            child = path + (name,)
            spec = specs.lookup(child)
            if spec is None:
                continue
            if spec.kind in {Kind.object, Kind.map}:
                if isinstance(value, Mapping):
                    walk(value, child)
            elif spec.kind is Kind.scalar:
                out.append(pointer(child))
            elif spec.strategy is Strategy.replace:
                out.append(pointer(child))
            elif spec.strategy is Strategy.union:
                for item in value:
                    out.append(pointer(child) + "/" + element_segment(str(item)))
            else:
                assert spec.key is not None
                for element in value:
                    walk(element, child + (str(element[spec.key]),))

    walk(values, ())
    return out


def flatten_leaves(
    specs: SpecIndex, values: Mapping[str, Any]
) -> list[tuple[str, Any]]:
    """(pointer, value) rows for display; union elements one row each."""
    rows: list[tuple[str, Any]] = []

    def walk(container: Mapping[str, Any], path: Path) -> None:
        for name, value in container.items():
            child = path + (name,)
            spec = specs.lookup(child)
            if spec is None:
                continue
            if spec.kind in {Kind.object, Kind.map}:
                if isinstance(value, Mapping):
                    walk(value, child)
            elif spec.kind is Kind.scalar or spec.strategy is Strategy.replace:
                rows.append((pointer(child), value))
            elif spec.strategy is Strategy.union:
                for item in value:
                    rows.append(
                        (pointer(child) + "/" + element_segment(str(item)), item)
                    )
            else:
                assert spec.key is not None
                for element in value:
                    walk(element, child + (str(element[spec.key]),))

    walk(values, ())
    return rows


def merge_all(
    module_key: str,
    specs: SpecIndex,
    patches: Sequence[tuple[Mapping[str, Any], Origin]],
) -> Merger:
    merger = Merger(module_key, specs)
    for patch, origin in patches:
        merger.merge_patch(patch, origin)
    return merger
