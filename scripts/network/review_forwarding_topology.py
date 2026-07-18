"""Review forwarding declarations and inspect their observation agreement."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.network.forwarding_topology import (  # noqa: E402
    ForwardingTopologyError,
    execute_forwarding_topology_decision,
    inspect_forwarding_topology_decision,
    preview_forwarding_topology_decision,
    propose_forwarding_topology_decision,
    reconcile_forwarding_topology,
    review_forwarding_topology_decision,
)


def _decision_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--action", choices=("declare", "retire"), required=True)
    parser.add_argument("--path-key", required=True)
    parser.add_argument(
        "--declaration-file",
        type=Path,
        help="JSON declaration payload; required for declare and forbidden for retire.",
    )
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Review exact Sub-owned forwarding declarations. This command never "
            "applies router configuration or derives path from observations."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="Hash exact current evidence.")
    _decision_arguments(preview)
    propose = commands.add_parser(
        "propose", help="Confirm and persist an exact preview."
    )
    _decision_arguments(propose)
    propose.add_argument("--expected-decision-sha256", required=True)

    for command in ("approve", "decline"):
        review = commands.add_parser(
            command, help=f"Independently {command} an exact proposal."
        )
        review.add_argument("--decision-id", required=True)
        review.add_argument("--expected-decision-sha256", required=True)
        review.add_argument("--actor", required=True)
        review.add_argument("--notes", required=True)

    execute = commands.add_parser(
        "execute", help="Apply an approved declaration after locked revalidation."
    )
    execute.add_argument("--decision-id", required=True)
    execute.add_argument("--expected-decision-sha256", required=True)
    execute.add_argument("--actor", required=True)
    inspect = commands.add_parser(
        "inspect", help="Inspect decision and exact result evidence."
    )
    inspect.add_argument("--decision-id", required=True)
    commands.add_parser(
        "audit",
        help="Read the declaration agreement/drift projection without writing.",
    )
    return parser.parse_args()


def _declaration(args: argparse.Namespace) -> dict[str, Any] | None:
    path = args.declaration_file
    if args.action == "retire":
        if path is not None:
            raise ForwardingTopologyError("--declaration-file is forbidden for retire")
        return None
    if path is None:
        raise ForwardingTopologyError("--declaration-file is required for declare")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForwardingTopologyError("declaration file is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ForwardingTopologyError("declaration file must contain a JSON object")
    return payload


def _preview_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "action": args.action,
        "declaration": _declaration(args),
        "path_key": args.path_key,
        "reason": args.reason,
        "proposed_by": args.actor,
    }


def main() -> int:
    args = parse_args()
    exit_code = 0
    try:
        with SessionLocal() as db:
            if args.command == "preview":
                output = preview_forwarding_topology_decision(
                    db, **_preview_args(args)
                ).to_dict()
            elif args.command == "propose":
                row = propose_forwarding_topology_decision(
                    db,
                    expected_decision_sha256=args.expected_decision_sha256,
                    **_preview_args(args),
                )
                output = inspect_forwarding_topology_decision(db, row.id)
            elif args.command in {"approve", "decline"}:
                row = review_forwarding_topology_decision(
                    db,
                    args.decision_id,
                    action=args.command,
                    reviewed_by=args.actor,
                    review_notes=args.notes,
                    expected_decision_sha256=args.expected_decision_sha256,
                )
                output = inspect_forwarding_topology_decision(db, row.id)
            elif args.command == "execute":
                row = execute_forwarding_topology_decision(
                    db,
                    args.decision_id,
                    executed_by=args.actor,
                    expected_decision_sha256=args.expected_decision_sha256,
                )
                output = inspect_forwarding_topology_decision(db, row.id)
            elif args.command == "inspect":
                output = inspect_forwarding_topology_decision(db, args.decision_id)
            else:
                report = reconcile_forwarding_topology(db)
                output = report.to_dict()
                exit_code = 0 if report.ready_for_operational_projection else 2
    except ForwardingTopologyError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(output, default=str, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
