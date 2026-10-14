"""Read-only drift report: compose desired state, compare with a stored export.

Pure over its inputs (no database, no router contact). The CLI adapter loads
the bindings and the latest ``RouterConfigSnapshot`` text and calls this.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.services.composition.compose import (
    Effective,
    LapsedException,
    compose,
    provenance_gaps,
)
from app.services.composition.drift import (
    DriftItem,
    ModuleEvaluation,
    ModuleStatus,
    NotEvaluated,
    Proposal,
    Unknown,
)
from app.services.composition.evaluate import evaluate_module
from app.services.composition.exceptions import ExceptionSpec
from app.services.composition.layers import LayerError, LayerRevision
from app.services.composition.merge import flatten_leaves
from app.services.router_health.routeros_export import ExportDocument, parse_export

from .plugin import ROUTER_PLUGIN, RouterBinding, binding_issues, build_subject

DEFAULT_MAX_AGE = timedelta(hours=48)

EXIT_CONFORM = 0
EXIT_DRIFT = 1
EXIT_REFUSED = 2
EXIT_NOTHING_EVALUATED = 3


@dataclass(frozen=True)
class SnapshotInput:
    """The latest stored ``/export`` for one router (text is never printed)."""

    snapshot_id: str | None
    captured_at: datetime | None
    text: str


class RouterOutcome(StrEnum):
    evaluated = "evaluated"
    skipped = "skipped"  # unmanaged / retired
    refused = "refused"  # invalid layers, exceptions or bindings


@dataclass
class RouterReport:
    binding: RouterBinding
    outcome: RouterOutcome
    issues: tuple[str, ...] = ()
    layers: tuple[dict[str, Any], ...] = ()
    effective: dict[str, Any] = field(default_factory=dict)
    evaluations: dict[str, ModuleEvaluation] = field(default_factory=dict)
    conflicts: tuple[dict[str, Any], ...] = ()
    inert: tuple[str, ...] = ()
    lapsed: tuple[LapsedException, ...] = ()
    router_hash: str | None = None
    snapshot: dict[str, Any] | None = None
    note: str | None = None

    @property
    def proposals(self) -> list[Proposal]:
        return [p for e in self.evaluations.values() for p in e.proposals]

    @property
    def items(self) -> list[DriftItem]:
        return [i for e in self.evaluations.values() for i in e.items]


@dataclass
class FleetReport:
    routers: list[RouterReport]
    today: date
    seed: str

    @property
    def exit_code(self) -> int:
        return exit_code(self)


def _origin_text(prov: Any) -> dict[str, Any]:
    out = prov.origin.as_dict()
    out["shadowed"] = [
        {"value": shadow.value, "origin": shadow.origin.label()}
        for shadow in prov.shadowed
    ]
    return out


def _effective_view(effective: Effective) -> dict[str, Any]:
    view: dict[str, Any] = {}
    for key, result in sorted(effective.modules.items()):
        specs = ROUTER_PLUGIN.specs(key)
        rows = []
        for ptr, value in flatten_leaves(specs, result.params):
            prov = result.provenance.get(ptr)
            rows.append(
                {
                    "path": ptr,
                    "value": value,
                    "origin": _origin_text(prov) if prov else None,
                }
            )
        view[key] = {
            "mode": result.mode,
            "status": result.status,
            "reasons": [r.as_dict() for r in result.reasons],
            "effective_hash": result.effective_hash,
            "values": rows,
            "tombstones": {
                ptr: tomb.deleted_by.label()
                for ptr, tomb in sorted(result.tombstones.items())
            },
            "provenance_gaps": provenance_gaps(ROUTER_PLUGIN, result),
        }
    return view


def _observation(
    snapshot: SnapshotInput | None, now: datetime, max_age: timedelta
) -> tuple[ExportDocument | Unknown, dict[str, Any] | None]:
    if snapshot is None:
        return Unknown(NotEvaluated.no_snapshot), None
    info = {
        "id": snapshot.snapshot_id,
        "captured_at": snapshot.captured_at.isoformat()
        if snapshot.captured_at
        else None,
    }
    if snapshot.captured_at is None or now - _aware(snapshot.captured_at) > max_age:
        return Unknown(NotEvaluated.stale, "snapshot older than the allowed age"), info
    doc = parse_export(snapshot.text)
    if not doc.parsed:
        return Unknown(NotEvaluated.unparseable, "export did not parse"), info
    return doc, info


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def build_router_report(
    binding: RouterBinding,
    snapshot: SnapshotInput | None,
    revisions: Sequence[LayerRevision],
    exceptions: Sequence[ExceptionSpec],
    *,
    today: date,
    now: datetime,
    max_age: timedelta = DEFAULT_MAX_AGE,
) -> RouterReport:
    bad = binding_issues([binding])
    if bad:
        return RouterReport(binding, RouterOutcome.refused, tuple(bad))
    if binding.lifecycle in {"unmanaged", "retired"}:
        return RouterReport(
            binding, RouterOutcome.skipped, note=f"lifecycle {binding.lifecycle}"
        )
    try:
        effective = compose(
            ROUTER_PLUGIN, build_subject(binding), revisions, exceptions, today
        )
    except LayerError as exc:
        return RouterReport(binding, RouterOutcome.refused, exc.issues)
    observation, info = _observation(snapshot, now, max_age)
    facts: dict[str, Any] = {
        "loopbacks": tuple(a for a in (binding.loopback_v4, binding.loopback_v6) if a),
        "loopback_v4": binding.loopback_v4,
    }
    if isinstance(observation, ExportDocument):
        facts["version"] = observation.version_tuple
        facts["identity"] = observation.identity
    evaluations: dict[str, ModuleEvaluation] = {}
    for key, result in sorted(effective.modules.items()):
        evaluation = evaluate_module(
            ROUTER_PLUGIN.modules[key], result, observation, facts
        )
        if (
            binding.lifecycle == "observe_only"
            and evaluation.status is ModuleStatus.drift
        ):
            evaluation = ModuleEvaluation(
                evaluation.module,
                evaluation.mode,
                ModuleStatus.observe_only,
                items=evaluation.items,
                proposals=evaluation.proposals,
            )
        evaluations[key] = evaluation
    conflicts = tuple(
        {"module": key, **c.as_dict()}
        for key, r in effective.modules.items()
        for c in r.conflicts
    )
    return RouterReport(
        binding=binding,
        outcome=RouterOutcome.evaluated,
        layers=tuple(
            {"tier": l.tier, "name": l.name, "weight": l.weight, "revision": l.revision}
            for l in effective.layers
        ),
        effective=_effective_view(effective),
        evaluations=evaluations,
        conflicts=conflicts,
        inert=effective.inert,
        lapsed=effective.lapsed,
        router_hash=effective.router_hash,
        snapshot=info,
    )


def build_fleet_report(
    bindings: Iterable[RouterBinding],
    snapshots: Mapping[str, SnapshotInput],
    revisions: Sequence[LayerRevision],
    exceptions: Sequence[ExceptionSpec],
    *,
    today: date,
    now: datetime,
    max_age: timedelta = DEFAULT_MAX_AGE,
    seed: str = "",
) -> FleetReport:
    routers = [
        build_router_report(
            binding,
            snapshots.get(binding.router_id),
            revisions,
            exceptions,
            today=today,
            now=now,
            max_age=max_age,
        )
        for binding in sorted(bindings, key=lambda b: (b.name, b.router_id))
    ]
    return FleetReport(routers, today, seed)


def exit_code(report: FleetReport) -> int:
    """0 conform, 1 drift, 2 refused (invalid input, conflict, unparseable), 3 nothing evaluated."""
    evaluated = 0
    drift = False
    for router in report.routers:
        if router.outcome is RouterOutcome.refused or router.conflicts:
            return EXIT_REFUSED
        for evaluation in router.evaluations.values():
            if evaluation.status is ModuleStatus.not_evaluated:
                if evaluation.reason in {
                    NotEvaluated.invalid_desired,
                    NotEvaluated.unparseable,
                }:
                    return EXIT_REFUSED
                continue
            if evaluation.status is ModuleStatus.off:
                continue
            evaluated += 1
            drift = drift or evaluation.status is ModuleStatus.drift
    if drift:
        return EXIT_DRIFT
    return EXIT_CONFORM if evaluated else EXIT_NOTHING_EVALUATED


# ------------------------------------------------------------------- rendering


def to_json(report: FleetReport) -> dict[str, Any]:
    return {
        "today": report.today.isoformat(),
        "seed": report.seed,
        "exit_code": report.exit_code,
        "routers": [
            {
                "router": r.binding.name,
                "router_id": r.binding.router_id,
                "outcome": r.outcome.value,
                "lifecycle": r.binding.lifecycle,
                "site": r.binding.site,
                "profiles": list(r.binding.profiles),
                "issues": list(r.issues),
                "note": r.note,
                "snapshot": r.snapshot,
                "layers": list(r.layers),
                "router_hash": r.router_hash,
                "effective": r.effective,
                "modules": {k: e.as_dict() for k, e in sorted(r.evaluations.items())},
                "proposals": [p.as_dict() for p in r.proposals],
                "conflicts": list(r.conflicts),
                "inert": list(r.inert),
                "lapsed": [x.as_dict() for x in r.lapsed],
            }
            for r in report.routers
        ],
    }


def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 70 else text[:67] + "..."


def render_text(report: FleetReport) -> str:
    lines = [
        f"router control report (read-only) {report.today.isoformat()} seed: {report.seed}"
    ]
    for r in report.routers:
        lines.append("")
        lines.append(
            f"== {r.binding.name}  site={r.binding.site or '-'}  "
            f"profiles={','.join(r.binding.profiles) or '-'}  {r.outcome.value}"
        )
        if r.note:
            lines.append(f"   {r.note}")
        for issue in r.issues:
            lines.append(f"   REFUSED: {issue}")
        if r.outcome is not RouterOutcome.evaluated:
            continue
        if r.snapshot:
            lines.append(
                f"   snapshot {r.snapshot.get('id')} at {r.snapshot.get('captured_at')}"
            )
        lines.append(
            "   layers: "
            + " > ".join(f"{l['tier']}:{l['name']}(r{l['revision']})" for l in r.layers)
        )
        lines.append(f"   router hash {r.router_hash}")
        for key, view in sorted(r.effective.items()):
            evaluation = r.evaluations.get(key)
            status = evaluation.status.value if evaluation else "-"
            reason = (
                f"({evaluation.reason.value})"
                if evaluation and evaluation.reason
                else ""
            )
            lines.append(
                f"   [{key}] mode={view['mode']} {view['status']} evaluation={status}{reason}"
                f" hash={view['effective_hash'][:12]}"
            )
            for reason_item in view["reasons"]:
                lines.append(
                    f"       invalid {reason_item['code']} {reason_item['path']} {reason_item['detail']}"
                )
            for row in view["values"]:
                origin = row["origin"]
                where = (
                    f"{origin['tier']} {origin['layer']} r{origin['revision']} ({origin['op']})"
                    if origin
                    else "?"
                )
                shadow = ""
                if origin and origin["shadowed"]:
                    shadow = "  shadowed: " + "; ".join(
                        f"{_short(s['value'])} by {s['origin']}"
                        for s in origin["shadowed"]
                    )
                lines.append(
                    f"       {row['path']} = {_short(row['value'])}  <- {where}{shadow}"
                )
            for ptr, who in view["tombstones"].items():
                lines.append(f"       {ptr} removed by {who}")
            if evaluation:
                for item in evaluation.items:
                    if item.status.value in {"ok"}:
                        continue
                    tag = " [excepted]" if item.excepted else ""
                    lines.append(
                        f"       DRIFT {item.status.value} {item.path}"
                        f"{'/' + item.element if item.element else ''}"
                        f" desired={_short(item.desired)} observed={_short(item.observed)}"
                        f" severity={item.severity}{tag}"
                    )
        for p in r.proposals:
            lines.append(
                f"   proposal {p.op} {p.module}{p.path} {_short(p.value)}"
                f"{'  WIDENING' if p.widening else ''}"
            )
        for c in r.conflicts:
            lines.append(
                f"   CONFLICT {c['module']}{c['path']} between {', '.join(c['layers'])}"
            )
        for path in r.inert:
            lines.append(f"   inert {path}")
        for x in r.lapsed:
            lines.append(f"   exception_lapsed {x.id} ({x.module}) until {x.until}")
    lines.append("")
    lines.append(f"exit {report.exit_code}")
    return "\n".join(lines)
