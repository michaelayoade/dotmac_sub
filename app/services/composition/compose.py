"""compose(): a pure function from layers + exceptions to the effective state."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .derive import NOT_DERIVED
from .document import (
    DELETE,
    DocumentError,
    element_segment,
    hash_of,
    pointer,
)
from .drift import Suppression
from .exceptions import (
    ExceptionKind,
    ExceptionSpec,
    ExceptionState,
    basic_exception_issues,
    exception_applies,
    exception_state,
)
from .fields import (
    OPTIONAL_UNMANAGED,
    REQUIRED,
    FieldSpec,
    Kind,
    Path,
    SpecIndex,
    Strategy,
    parse_spec_pointer,
)
from .layers import (
    Layer,
    LayerError,
    LayerRevision,
    Subject,
    layer_applies,
    sort_key,
    validate_layer_meta,
)
from .merge import Conflict, Merger, Origin, Provenance, Tombstone, leaf_pointers
from .plugin import MODES, DomainPlugin, Invalid
from .widening import mode_widening, params_widening

SCHEMA_VERSION = 1
_BLOCK_KEYS = frozenset({"module_version", "mode", "params"})


# ------------------------------------------------------------------ result types


@dataclass(frozen=True)
class AppliedLayer:
    tier: str
    name: str
    layer_id: str
    revision: int
    weight: int


@dataclass(frozen=True)
class LapsedException:
    id: str
    module: str
    until: date | None
    reason: str
    state: ExceptionState

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "module": self.module,
            "until": self.until.isoformat() if self.until else None,
            "reason": self.reason,
            "state": self.state.value,
            "finding": "exception_lapsed",
        }


@dataclass(frozen=True)
class ExceptionStatus:
    """What compose() decided about one applicable exception (widening is computed)."""

    id: str
    module: str
    kind: str
    state: ExceptionState
    widening: bool
    approved: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "module": self.module,
            "kind": self.kind,
            "state": self.state.value,
            "widening": self.widening,
            "approved": self.approved,
        }


@dataclass
class ModuleResult:
    key: str
    version: int
    mode: str
    params: dict[str, Any]
    status: str  # ok | invalid_desired
    reasons: tuple[Invalid, ...]
    conflicts: tuple[Conflict, ...]
    provenance: dict[str, Provenance]
    tombstones: dict[str, Tombstone]
    suppressions: tuple[Suppression, ...]
    effective_hash: str
    lints: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def blocked(self) -> bool:
        """Planning is refused: invalid desired state or an unresolved conflict."""
        return not self.ok or bool(self.conflicts)

    def origin_for(self, ptr: str) -> Origin | None:
        current = ptr
        while True:
            hit = self.provenance.get(current)
            if hit:
                return hit.origin
            if not current:
                return None
            current = current.rsplit("/", 1)[0]


@dataclass
class Effective:
    subject_key: str
    today: date
    layers: tuple[AppliedLayer, ...]
    modules: dict[str, ModuleResult]
    inert: tuple[str, ...]
    lapsed: tuple[LapsedException, ...]
    annotations: tuple[tuple[str, str, str], ...]
    router_hash: str
    exceptions: tuple[ExceptionStatus, ...] = ()

    def conflicts(self) -> list[Conflict]:
        return [c for r in self.modules.values() for c in r.conflicts]


# ------------------------------------------------------------- write validation


def _walk_patch(
    specs: SpecIndex, patch: Mapping[str, Any], path: Path
) -> Iterator[tuple[Path, FieldSpec | None, Any]]:
    for key, value in patch.items():
        child = path + (str(key),)
        spec = specs.lookup(child)
        yield child, spec, value
        if spec is None:
            continue
        if (
            spec.kind in {Kind.object, Kind.map}
            and isinstance(value, Mapping)
            and not is_delete_safe(value)
        ):
            yield from _walk_patch(specs, value, child)
        elif spec.kind is Kind.list and spec.strategy is Strategy.keyed:
            items = value.get("$replace", []) if isinstance(value, Mapping) else value
            if isinstance(items, (list, tuple)):
                for element in items:
                    if isinstance(element, Mapping) and spec.key in element:
                        body = {
                            k: v
                            for k, v in element.items()
                            if k != spec.key and k != DELETE
                        }
                        yield from _walk_patch(
                            specs, body, child + (str(element[spec.key]),)
                        )


def is_delete_safe(value: Mapping[str, Any]) -> bool:
    return value.get(DELETE) is True


def _type_issue(annotation: Any, value: Any) -> bool:
    try:
        TypeAdapter(annotation).validate_python(value, strict=True)
    except ValidationError:
        return True
    return False


def validate_layer_document(
    plugin: DomainPlugin, layer: Layer, document: Mapping[str, Any]
) -> list[str]:
    """Write-time validation (N7): shape, directives, canonical values, types."""
    issues = validate_layer_meta(plugin.tier(layer.tier), layer)
    where = f"layer {layer.name}"
    if not isinstance(document, Mapping):
        return issues + [f"{where}: document must be an object"]
    if document.get("schema") != SCHEMA_VERSION:
        issues.append(f"{where}: schema must be {SCHEMA_VERSION}")
    extra = sorted(set(document) - {"schema", "modules"})
    if extra:
        issues.append(f"{where}: unknown top-level keys {extra}")
    modules = document.get("modules", {})
    if not isinstance(modules, Mapping):
        return issues + [f"{where}: modules must be an object"]
    rank = plugin.tier(layer.tier).rank
    for key, block in sorted(modules.items()):
        if key not in plugin.modules:
            issues.append(f"{where}: unknown module {key!r}")
            continue
        if is_delete_safe(block) if isinstance(block, Mapping) else False:
            issues.append(
                f"{where}: module {key} cannot be deleted; use mode: off (M7)"
            )
            continue
        if not isinstance(block, Mapping):
            issues.append(f"{where}: module {key} must be an object")
            continue
        stray = sorted(set(block) - _BLOCK_KEYS)
        if stray:
            issues.append(f"{where}: module {key}: unknown keys {stray}")
        module = plugin.modules[key]
        if not isinstance(block.get("module_version"), int):
            issues.append(f"{where}: module {key}: module_version is required")
        mode = block.get("mode")
        if mode is not None and mode not in MODES:
            issues.append(f"{where}: module {key}: mode must be one of {list(MODES)}")
        if block.get("module_version") != module.version:
            # T23: a stale block is reported as invalid_desired(stale_layer) for
            # this module by compose(); its parameters belong to another schema
            # version, so they are not validated against this one.
            continue
        params = block.get("params", {})
        if not isinstance(params, Mapping):
            issues.append(f"{where}: module {key}: params must be an object")
            continue
        specs = plugin.specs(key)
        probe = Merger(key, specs)
        origin = Origin("probe", rank, layer.id, layer.name, 0, layer.weight)
        try:
            probe.merge_patch(params, origin)
        except DocumentError as exc:
            issues.append(f"{where}: module {key}: {exc}")
            continue
        annotations = plugin.leaf_annotations(key)
        for path, spec, value in _walk_patch(specs, params, ()):
            if spec is None:
                continue
            ptr = f"module {key}{pointer(path)}"
            if spec.derived:
                issues.append(f"{where}: {ptr} is derived and cannot be set")
            if is_delete_safe(value) if isinstance(value, Mapping) else False:
                if rank == 0 and spec.default is REQUIRED:
                    issues.append(
                        f"{where}: {ptr} is required at the global tier and cannot be deleted"
                    )
                continue
            if spec.secret:
                if not (isinstance(value, Mapping) and "secret_ref" in value):
                    issues.append(f"{where}: {ptr} accepts only a secret reference")
                continue
            if spec.kind is Kind.scalar and value is not None:
                annotation = annotations.get(specs.resolve(path))
                if annotation is not None and _type_issue(
                    annotation, _canon_for_check(spec, value)
                ):
                    issues.append(f"{where}: {ptr} has the wrong type")
            elif spec.kind is Kind.list and spec.strategy is not Strategy.keyed:
                annotation = annotations.get(specs.resolve(path))
                elements = _list_elements(value)
                for element in elements:
                    if annotation is not None and _type_issue(
                        annotation, _canon_for_check(spec, element)
                    ):
                        issues.append(
                            f"{where}: {ptr} has an element of the wrong type"
                        )
                        break
    return issues


def _list_elements(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        out: list[Any] = []
        for key in ("$add", "$remove", "$replace"):
            out.extend(value.get(key, []) or [])
        return out
    return list(value) if isinstance(value, (list, tuple)) else []


def _canon_for_check(spec: FieldSpec, value: Any) -> Any:
    try:
        return spec.canonicalise(value)
    except (ValueError, TypeError):
        return value


def validate_exception(plugin: DomainPlugin, exc: ExceptionSpec) -> list[str]:
    """Write-time validation of an exception, including X3 (no derived targets)."""
    issues = basic_exception_issues(exc)
    if exc.module_key not in plugin.modules:
        return issues + [f"exception {exc.id}: unknown module {exc.module_key!r}"]
    specs = plugin.specs(exc.module_key)
    if exc.kind is ExceptionKind.override and exc.patch is not None:
        try:
            base_path = tuple(parse_spec_pointer(exc.path))
            nested = _nest(base_path, exc.patch)
        except (ValueError, DocumentError) as exc_err:
            return issues + [f"exception {exc.id}: {exc_err}"]
        try:
            Merger(exc.module_key, specs).merge_patch(
                nested, Origin("probe", 0, exc.id, exc.id, 0, 0)
            )
        except DocumentError as exc_err:
            issues.append(f"exception {exc.id}: {exc_err}")
            return issues
        for path, spec, _value in _walk_patch(specs, nested, ()):
            if spec is not None and spec.derived:
                issues.append(
                    f"exception {exc.id}: {pointer(path)} is derived; override its source instead (X3)"
                )
    return issues


def _nest(path: Path, patch: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(patch, Mapping):
        raise DocumentError(pointer(path), "an override patch must be an object")
    nested: dict[str, Any] = dict(patch)
    for segment in reversed(path):
        nested = {segment: nested}
    return nested


# ------------------------------------------------------------------------ compose


def _default_origin(module_key: str, version: int) -> Origin:
    return Origin(
        "default",
        -1,
        f"default:{module_key}",
        f"{module_key}@{version}",
        version,
        0,
        "default",
    )


def _get_path(values: Mapping[str, Any], path: Path) -> Any:
    cursor: Any = values
    for segment in path:
        if not isinstance(cursor, Mapping) or segment not in cursor:
            return None
        cursor = cursor[segment]
    return cursor


def _fill_defaults(
    merger: Merger,
    specs: SpecIndex,
    container: dict[str, Any],
    path: Path,
    origin: Origin,
    missing: list[str],
) -> None:
    for name, spec in specs.children(specs.resolve(path)):
        if name == "*" or spec.derived:
            continue
        child = path + (name,)
        ptr = pointer(child)
        if spec.kind is Kind.object:
            existing = container.get(name)
            if existing is None and spec.default is OPTIONAL_UNMANAGED:
                continue  # an absent optional object is not conjured by defaults
            created = not isinstance(existing, dict)
            sub: dict[str, Any] = {} if not isinstance(existing, dict) else existing
            _fill_defaults(merger, specs, sub, child, origin, missing)
            if created and sub:
                container[name] = sub
        elif spec.kind is Kind.map:
            members = container.get(name)
            if isinstance(members, dict):
                for key, value in members.items():
                    if isinstance(value, dict):
                        _fill_defaults(
                            merger, specs, value, child + (key,), origin, missing
                        )
        else:
            present = name in container
            if present:
                if (
                    spec.kind is Kind.list
                    and spec.strategy is Strategy.keyed
                    and spec.key
                ):
                    for element in container[name]:
                        _fill_defaults(
                            merger,
                            specs,
                            element,
                            child + (str(element[spec.key]),),
                            origin,
                            missing,
                        )
                continue
            if spec.has_value_default:
                value = copy.deepcopy(spec.default)
                container[name] = value
                merger.prov[ptr] = Provenance(
                    origin=origin, value=value, contributors=(origin,)
                )
                if spec.kind is Kind.list and spec.strategy is Strategy.union:
                    for item in value:
                        merger.prov[ptr + "/" + element_segment(str(item))] = (
                            Provenance(
                                origin=origin, value=item, contributors=(origin,)
                            )
                        )
            elif spec.default is REQUIRED:
                missing.append(ptr)


def provenance_gaps(plugin: DomainPlugin, result: ModuleResult) -> list[str]:
    """Completeness (guard 9): every effective leaf has exactly one origin."""
    specs = plugin.specs(result.key)
    return [
        ptr
        for ptr in leaf_pointers(specs, result.params)
        if ptr not in result.provenance
    ]


def compose(
    plugin: DomainPlugin,
    subject: Subject,
    revisions: Iterable[LayerRevision],
    exceptions: Iterable[ExceptionSpec],
    today: date,
) -> Effective:
    """Pure: the same inputs give the same effective state in any input order."""
    top_rank = max(t.rank for t in plugin.tiers)
    # 1. applicable, active layers in (rank, weight, name, id) order
    chosen: list[LayerRevision] = []
    seen_layers: set[str] = set()
    for revision in revisions:
        tier = plugin.tier(revision.layer.tier)
        if layer_applies(tier, revision.layer, subject):
            if revision.layer.id in seen_layers:
                raise LayerError(
                    [f"layer {revision.layer.name}: more than one active revision"]
                )
            seen_layers.add(revision.layer.id)
            chosen.append(revision)
    chosen.sort(key=lambda r: sort_key(plugin.tier(r.layer.tier), r.layer))
    issues: list[str] = []
    for revision in chosen:
        issues.extend(
            validate_layer_document(plugin, revision.layer, revision.document)
        )
    if issues:
        raise LayerError(issues)

    def origin_of(revision: LayerRevision) -> Origin:
        return Origin(
            revision.layer.tier,
            plugin.tier(revision.layer.tier).rank,
            revision.layer.id,
            revision.layer.name,
            revision.revision,
            revision.layer.weight,
            "set",
            revision.activated_at,
        )

    # 2-3. enrolment is a union (P3); modes are merged like scalars
    mergers: dict[str, Merger] = {}
    stale: dict[str, list[Invalid]] = {}
    for revision in chosen:
        for key, block in sorted(revision.document.get("modules", {}).items()):
            if block.get("mode") is None:
                continue
            merger = mergers.setdefault(key, Merger(key, plugin.specs(key)))
            merger.set_mode(block["mode"], origin_of(revision))
    inert: list[str] = []
    for revision in chosen:
        origin = origin_of(revision)
        for key, block in sorted(revision.document.get("modules", {}).items()):
            params = block.get("params") or {}
            if key not in mergers:
                inert.extend(f"/{key}{p}" for p in _patch_pointers(params))
                continue
            module = plugin.modules[key]
            if block.get("module_version") != module.version:
                stale.setdefault(key, []).append(
                    Invalid(
                        "stale_layer",
                        f"/{key}",
                        f"layer block is version {block.get('module_version')}, module is {module.version}",
                        origin,
                    )
                )
                continue
            try:
                mergers[key].merge_patch(params, origin)
            except DocumentError as err:
                raise LayerError(
                    [f"layer {revision.layer.name}: module {key}: {err}"]
                ) from err

    # 4. exceptions, in (created_on, id) order
    ordered_exceptions = sorted(exceptions, key=lambda e: e.sort_key)
    exc_issues: list[str] = []
    for exc in ordered_exceptions:
        exc_issues.extend(validate_exception(plugin, exc))
    if exc_issues:
        raise LayerError(exc_issues)
    lapsed: list[LapsedException] = []
    annotations: list[tuple[str, str, str]] = []
    suppressions: dict[str, list[Suppression]] = {}
    statuses: list[ExceptionStatus] = []
    for exc in ordered_exceptions:
        if not exception_applies(exc, subject.key, subject.dims):
            continue
        state = exception_state(exc, today, widening=False)  # time / revoked only
        if state is ExceptionState.lapsed:
            lapsed.append(
                LapsedException(exc.id, exc.module_key, exc.until, exc.reason, state)
            )
            continue
        if state is not ExceptionState.active:
            statuses.append(
                ExceptionStatus(
                    exc.id, exc.module_key, exc.kind.value, state, False, exc.approved
                )
            )
            continue
        exc_merger = mergers.get(exc.module_key)
        if exc_merger is None:
            continue
        origin = Origin(
            "exception",
            top_rank + 1,
            f"exception:{exc.id}",
            exc.id,
            0,
            0,
            "exception",
            None,
            {
                "reason": exc.reason,
                "until": exc.until.isoformat() if exc.until else None,
            },
        )
        widened = False
        if exc.kind in {ExceptionKind.override, ExceptionKind.mode_override}:
            # widening is computed from the module before and after the
            # exception, never taken from the input (§1.7, X1)
            trial = copy.deepcopy(exc_merger)
            _apply_exception(trial, exc, origin)
            widened = _exception_widens(plugin, exc.module_key, exc_merger, trial)
        state = exception_state(exc, today, widening=widened)
        statuses.append(
            ExceptionStatus(
                exc.id, exc.module_key, exc.kind.value, state, widened, exc.approved
            )
        )
        if state is not ExceptionState.active:
            continue
        if exc.kind in {ExceptionKind.override, ExceptionKind.mode_override}:
            _apply_exception(exc_merger, exc, origin)
        elif exc.kind is ExceptionKind.suppress:
            suppressions.setdefault(exc.module_key, []).append(
                Suppression(
                    exc.id,
                    exc.module_key,
                    exc.scope or "*",
                    exc.reason,
                    exc.until.isoformat() if exc.until else None,
                )
            )
        else:
            annotations.append((exc.module_key, exc.reason, exc.id))

    # 5. derived values, in dependency order
    for rule in plugin.all_rules():
        target = mergers.get(rule.module)
        if target is None:
            continue
        inputs: dict[str, Any] = {}
        described: list[dict[str, Any]] = []
        for ref in rule.reads:
            if ref.module is None:
                inputs[ref.label] = subject.facts.get(ref.path)
                described.append({"ref": ref.label, "origin": "subject fact"})
                continue
            source = mergers.get(ref.module)
            if source is None:
                inputs[ref.label] = None
                described.append({"ref": ref.label, "origin": "not enrolled"})
                continue
            inputs[ref.label] = copy.deepcopy(
                _get_path(source.values, tuple(parse_spec_pointer(ref.path)))
            )
            origin_in = source.origin_for(ref.path)
            described.append(
                {
                    "ref": ref.label,
                    "origin": origin_in.label() if origin_in else "default",
                }
            )
        value = rule.fn(inputs)
        if value is NOT_DERIVED:
            continue
        target.set_derived(
            tuple(parse_spec_pointer(rule.target)),
            value,
            Origin(
                "derived",
                top_rank + 2,
                f"derived:{rule.name}",
                rule.name,
                0,
                0,
                "derived",
                None,
                {"rule": rule.name, "inputs": described},
            ),
        )

    # 6. defaults, schema validation, invariants, claims
    results: dict[str, ModuleResult] = {}
    claims: dict[str, frozenset[str]] = {}
    pending: dict[str, tuple[Merger, list[Invalid], bool]] = {}
    for key in sorted(mergers):
        merger = mergers[key]
        module = plugin.modules[key]
        specs = plugin.specs(key)
        missing: list[str] = []
        _fill_defaults(
            merger,
            specs,
            merger.values,
            (),
            _default_origin(key, module.version),
            missing,
        )
        reasons: list[Invalid] = list(stale.get(key, []))
        reasons.extend(
            Invalid("missing_required", ptr, "no layer sets this required field")
            for ptr in missing
        )
        schema_ok = False
        if not missing:
            try:
                module.params_schema.model_validate(merger.values)
                schema_ok = True
            except ValidationError as err:
                for item in err.errors():
                    loc = pointer([str(part) for part in item["loc"]])
                    reasons.append(
                        Invalid("schema", loc, str(item["msg"]), merger.origin_for(loc))
                    )
        if schema_ok:
            for inv in module.invariants(merger.values, subject.facts):
                reasons.append(replace_origin(inv, merger))
            claims[key] = module.claims(merger.values)
        pending[key] = (merger, reasons, schema_ok)
    claimed_by: dict[str, list[str]] = {}
    for key, claim_set in claims.items():
        for claim in claim_set:
            claimed_by.setdefault(claim, []).append(key)
    for claim, owners in sorted(claimed_by.items()):
        if len(owners) > 1:
            for owner in owners:
                pending[owner][1].append(
                    Invalid(
                        "double_claim", "", f"{claim} is claimed by {sorted(owners)}"
                    )
                )
    for key in sorted(pending):
        merger, reasons, _ok = pending[key]
        module = plugin.modules[key]
        mode = merger.mode or "off"
        reasons.sort(key=lambda r: (r.code, r.path, r.detail))
        results[key] = ModuleResult(
            key=key,
            version=module.version,
            mode=mode,
            params=merger.values,
            status="invalid_desired" if reasons else "ok",
            reasons=tuple(reasons),
            conflicts=tuple(sorted(merger.conflicts.values(), key=lambda c: c.path)),
            provenance=merger.prov,
            tombstones=merger.tombs,
            suppressions=tuple(suppressions.get(key, ())),
            effective_hash=hash_of(
                {
                    "module": key,
                    "version": module.version,
                    "mode": mode,
                    "params": merger.values,
                }
            ),
            lints=tuple(merger.lints),
        )
    return Effective(
        subject_key=subject.key,
        today=today,
        layers=tuple(
            AppliedLayer(
                r.layer.tier, r.layer.name, r.layer.id, r.revision, r.layer.weight
            )
            for r in chosen
        ),
        modules=results,
        inert=tuple(sorted(set(inert))),
        lapsed=tuple(lapsed),
        annotations=tuple(annotations),
        router_hash=hash_of({k: r.effective_hash for k, r in sorted(results.items())}),
        exceptions=tuple(statuses),
    )


def _apply_exception(merger: Merger, exc: ExceptionSpec, origin: Origin) -> None:
    if exc.kind is ExceptionKind.override:
        assert exc.patch is not None
        merger.merge_patch(
            _nest(tuple(parse_spec_pointer(exc.path)), exc.patch), origin
        )
    elif exc.kind is ExceptionKind.mode_override:
        assert exc.mode is not None
        merger.set_mode(exc.mode, origin)


def _exception_widens(
    plugin: DomainPlugin, module_key: str, before: Merger, after: Merger
) -> bool:
    """True when applying the exception loosens the module's security posture."""
    if params_widening(plugin.specs(module_key), before.values, after.values):
        return True
    return plugin.modules[module_key].mode_ordered and mode_widening(
        before.mode, after.mode
    )


def replace_origin(inv: Invalid, merger: Merger) -> Invalid:
    if inv.origin is not None or not inv.path:
        return inv
    return Invalid(inv.code, inv.path, inv.detail, merger.origin_for(inv.path))


def _patch_pointers(params: Mapping[str, Any], prefix: Path = ()) -> list[str]:
    out: list[str] = []
    for key, value in params.items():
        child = prefix + (str(key),)
        if (
            isinstance(value, Mapping)
            and not any(str(k).startswith("$") for k in value)
            and value
        ):
            out.extend(_patch_pointers(value, child))
        else:
            out.append(pointer(child))
    return out
