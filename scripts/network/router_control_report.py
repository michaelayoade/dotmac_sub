"""Compose each router's desired state and report drift against its latest export.

Read-only adapter for ``app.services.router_control``: a REPEATABLE READ READ
ONLY session, rolled back. It never connects to a router, never writes to the
database and never prints export text, community names or secrets. The desired
state is a PROPOSED synthetic seed unless ``--layers`` supplies a private seed.

    python -m scripts.network.router_control_report [--router NAME ...]
        [--layers FILE] [--exceptions FILE] [--assignments FILE] [--sites FILE]
        [--today YYYY-MM-DD] [--max-age-hours 48] [--json]

Without ``--assignments`` a router's profile is its current health role from
``network.router_health_policy`` and its site comes from ``--sites``.

Exit codes: 0 every evaluated module conforms; 1 drift (including drift in a
module in observe mode or on an ``observe_only`` router, which is labelled
"observe-only" and not enforced but is still drift); 2 refused (invalid input,
composition conflict, invalid desired state, unparseable export, or any
unexpected error: the CLI never exits 1 for a failure); 3 nothing could be
evaluated.

"Today" is the date in the application timezone (Africa/Lagos), not UTC.
Without a loopback in ``--assignments`` the ntp self-reference check cannot run
and the report says so for that router.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import read_only_snapshot_session
from app.models.router_management import Router, RouterConfigSnapshot
from app.services.router_control import inputs, report
from app.services.router_control.plugin import RouterBinding
from app.services.router_control.seed_layers import SEED_STATUS, seed_layer_revisions
from app.services.router_health.policy import (
    effective_policy,
    load_policy,
    today_in_app_timezone,
)
from app.services.router_health.routeros_export import parse_export


def _load_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise inputs.InputError(f"cannot read {path}: {type(exc).__name__}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else ""
    )
    parser.add_argument(
        "--router", action="append", default=[], help="router name (repeatable)"
    )
    parser.add_argument(
        "--layers", help="private layers JSON (replaces the synthetic seed)"
    )
    parser.add_argument("--exceptions", help="exceptions JSON")
    parser.add_argument("--assignments", help="per-router site/profiles/tags JSON")
    parser.add_argument(
        "--sites", help="router name -> site JSON (default assignments)"
    )
    parser.add_argument("--today", help="ISO date for exception lapse tests")
    parser.add_argument("--max-age-hours", type=float, default=48.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def load_fleet(
    db: Session,
    names: Sequence[str],
    assignments: Mapping[str, Mapping[str, Any]],
    sites: Mapping[str, str],
    today: date,
) -> tuple[list[RouterBinding], dict[str, report.SnapshotInput], list[str]]:
    """Read active routers and their latest snapshot (read-only)."""
    query = select(Router).where(Router.is_active.is_(True)).order_by(Router.name)
    rows = list(db.scalars(query))
    wanted = set(names)
    unknown = sorted(wanted - {row.name for row in rows})
    loaded = load_policy(db)
    warnings = (
        [f"router health policy invalid, defaults used: {loaded.error}"]
        if loaded.error
        else []
    )
    bindings: list[RouterBinding] = []
    snapshots: dict[str, report.SnapshotInput] = {}
    for row in rows:
        if wanted and row.name not in wanted:
            continue
        router_id = str(row.id)
        snap = db.scalars(
            select(RouterConfigSnapshot)
            .where(RouterConfigSnapshot.router_id == row.id)
            .order_by(RouterConfigSnapshot.created_at.desc())
            .limit(1)
        ).first()
        if snap is not None:
            snapshots[router_id] = report.SnapshotInput(
                str(snap.id), snap.created_at, snap.config_export
            )
        if row.name in assignments:
            bindings.append(
                inputs.parse_assignment(row.name, router_id, assignments[row.name])
            )
            continue
        identity = (
            parse_export(snap.config_export).identity if snap is not None else None
        )
        role = effective_policy(
            loaded.policy,
            identity=identity,
            router_name=row.name,
            router_id=row.id,
            today=today,
        ).role
        bindings.append(
            RouterBinding(
                router_id=router_id,
                name=row.name,
                site=sites.get(row.name),
                profiles=(role.value,),
            )
        )
    return (
        bindings,
        snapshots,
        [f"unknown router: {name}" for name in unknown] + warnings,
    )


def default_today() -> date:
    """The application-timezone date (exceptions lapse at Lagos midnight, X1)."""
    return today_in_app_timezone()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the report. Every failure that is not drift exits 2 (refused)."""
    try:
        return _run(argv)
    except SystemExit:
        raise  # argparse usage errors already exit 2
    except inputs.InputError as exc:
        print(f"refused: {exc}", file=sys.stderr)
    except Exception as exc:
        # fail closed: never leak the message (it may carry secrets) and never
        # report an internal failure as drift (exit 1)
        print(f"refused: unexpected {type(exc).__name__}", file=sys.stderr)
    return report.EXIT_REFUSED


def _run(argv: Sequence[str] | None) -> int:
    args = _parser().parse_args(argv)
    try:
        today = date.fromisoformat(args.today) if args.today else default_today()
    except ValueError as exc:
        raise inputs.InputError("--today: not an ISO date") from exc
    revisions = (
        inputs.parse_layers(_load_json(args.layers))
        if args.layers
        else seed_layer_revisions()
    )
    seed = f"private layers {args.layers}" if args.layers else SEED_STATUS
    assignments = (
        inputs.parse_assignments(_load_json(args.assignments))
        if args.assignments
        else {}
    )
    sites = inputs.parse_sites(_load_json(args.sites)) if args.sites else {}
    exceptions_raw = _load_json(args.exceptions) if args.exceptions else None
    with read_only_snapshot_session() as db:
        bindings, snapshots, notes = load_fleet(
            db, args.router, assignments, sites, today
        )
    unknown = [n for n in notes if n.startswith("unknown router")]
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    if unknown:
        return report.EXIT_REFUSED
    unchecked = [b.name for b in bindings if not (b.loopback_v4 or b.loopback_v6)]
    if unchecked:
        print(
            "note: no loopback declared for "
            f"{len(unchecked)} router(s) (default assignment or none in "
            "--assignments): the ntp self-reference check cannot run for them",
            file=sys.stderr,
        )
    exceptions = (
        inputs.parse_exceptions(
            exceptions_raw,
            {
                **{b.name: b.router_id for b in bindings},
                **{b.router_id: b.router_id for b in bindings},
            },
        )
        if exceptions_raw is not None
        else []
    )
    fleet = report.build_fleet_report(
        bindings,
        snapshots,
        revisions,
        exceptions,
        today=today,
        now=datetime.now(UTC),
        max_age=timedelta(hours=args.max_age_hours),
        seed=seed,
    )
    if args.as_json:
        print(json.dumps(report.to_json(fleet), indent=2, sort_keys=True, default=str))
    else:
        print(report.render_text(fleet))
    return fleet.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
