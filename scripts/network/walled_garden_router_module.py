"""Render the walled-garden router module and report per-router readiness.

Operator adapter for ``access.walled_garden_router_module`` and
``access.walled_garden_router_readiness``. Read-only: REPEATABLE READ READ ONLY
session, rolled back; it never connects to a router and never pushes config.

    python -m scripts.network.walled_garden_router_module render [--format script|rest|legacy]
    python -m scripts.network.walled_garden_router_module readiness [--router NAME] [--max-age-hours 48]

Exit codes: 0 success (render) or every evaluated router ready (readiness);
1 at least one router not ready; 2 the module cannot be rendered or the named
router does not exist.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta

from sqlalchemy import select

from app.db import read_only_snapshot_session
from app.models.router_management import Router
from app.services.walled_garden_router_module import (
    WalledGardenModuleError,
    render_walled_garden_module_from_settings,
)
from app.services.walled_garden_router_readiness import (
    FleetWalledGardenReadinessQuery,
    WalledGardenReadinessError,
    WalledGardenReadinessQuery,
    WalledGardenRouterReadiness,
    resolve_fleet_walled_garden_readiness,
    resolve_router_walled_garden_readiness,
)

EXIT_OK = 0
EXIT_NOT_READY = 1
EXIT_REFUSED = 2


def _readiness_row(item: WalledGardenRouterReadiness) -> dict[str, object]:
    return {
        "router": item.router_name,
        "router_id": str(item.router_id),
        "status": item.status.value,
        "module_version": item.module_version,
        "snapshot_id": str(item.snapshot_id) if item.snapshot_id else None,
        "snapshot_captured_at": (
            item.snapshot_captured_at.isoformat() if item.snapshot_captured_at else None
        ),
        "missing_elements": list(item.missing_elements),
        "missing_entry_keys": list(item.missing_entry_keys),
        "drifted_entry_keys": list(item.drifted_entry_keys),
        "findings": [
            {
                "element": finding.element_tag,
                "issue": finding.issue.value,
                "entry_key": finding.entry_key,
                "detail": finding.detail,
            }
            for finding in item.findings
        ],
        "legacy_rules_present": list(item.legacy_rules_present),
        "legacy_rule_count": item.legacy_rule_count,
        "static_suspended_enabled": item.static_suspended_enabled,
        "static_suspended_disabled": item.static_suspended_disabled,
        "configuration_error": item.configuration_error,
    }


def _render(output_format: str) -> int:
    with read_only_snapshot_session() as db:
        try:
            module = render_walled_garden_module_from_settings(db)
        except WalledGardenModuleError as exc:
            print(f"refused: {exc.code.value}: {exc.message}", file=sys.stderr)
            return EXIT_REFUSED
    if output_format == "rest":
        print("\n".join(module.rest_commands()))
    elif output_format == "legacy":
        print(
            json.dumps(
                [
                    {
                        "resource": item.resource.value,
                        "chain": item.chain,
                        "comment": item.comment,
                        "role": item.role.value,
                    }
                    for item in module.legacy_elements_to_retire
                ],
                indent=2,
            )
        )
    else:
        print(module.routeros_script(), end="")
    return EXIT_OK


def _readiness(router_name: str | None, max_age_hours: float) -> int:
    max_age = timedelta(hours=max_age_hours)
    with read_only_snapshot_session() as db:
        if router_name:
            router_id = db.scalars(
                select(Router.id).where(Router.name == router_name)
            ).first()
            if router_id is None:
                print(f"refused: router {router_name!r} not found", file=sys.stderr)
                return EXIT_REFUSED
            try:
                results = (
                    resolve_router_walled_garden_readiness(
                        db,
                        query=WalledGardenReadinessQuery(
                            router_id=router_id, max_snapshot_age=max_age
                        ),
                    ),
                )
            except WalledGardenReadinessError as exc:
                print(f"refused: {exc.code.value}", file=sys.stderr)
                return EXIT_REFUSED
        else:
            fleet = resolve_fleet_walled_garden_readiness(
                db, query=FleetWalledGardenReadinessQuery(max_snapshot_age=max_age)
            )
            results = fleet.routers
    print(json.dumps([_readiness_row(item) for item in results], indent=2))
    return EXIT_OK if all(item.is_ready for item in results) else EXIT_NOT_READY


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render", help="print the rendered v1 module")
    render.add_argument(
        "--format", choices=("script", "rest", "legacy"), default="script"
    )
    readiness = sub.add_parser("readiness", help="per-router readiness report")
    readiness.add_argument("--router", default=None)
    readiness.add_argument("--max-age-hours", type=float, default=48.0)
    args = parser.parse_args(argv)
    if args.command == "render":
        return _render(args.format)
    return _readiness(args.router, args.max_age_hours)


if __name__ == "__main__":
    raise SystemExit(main())
