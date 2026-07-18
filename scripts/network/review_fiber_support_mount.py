"""Preview and review exact canonical fiber asset-to-support mount commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.network.fiber_support_structures import (  # noqa: E402
    FiberSupportStructureError,
    execute_mount_decision,
    inspect_mount_decision,
    preview_mount_decision,
    propose_mount_decision,
    review_mount_decision,
)


def _mount_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--action", choices=("attach", "detach"), required=True)
    parser.add_argument("--support-id", required=True)
    parser.add_argument(
        "--asset-type",
        choices=(
            "fdh_cabinet",
            "fiber_access_point",
            "splice_closure",
            "fiber_segment",
        ),
        required=True,
    )
    parser.add_argument("--asset-id", required=True)
    parser.add_argument(
        "--mount-role", choices=("hosted", "route_support", "anchor"), required=True
    )
    parser.add_argument("--sequence", type=int)
    parser.add_argument("--existing-mount-id")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Review exact support mounts without deriving edges from map geometry, "
            "names, or proximity."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="Hash exact current evidence.")
    _mount_args(preview)
    propose = commands.add_parser(
        "propose", help="Confirm and persist an exact preview."
    )
    _mount_args(propose)
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
        "execute", help="Apply an approved proposal after locked revalidation."
    )
    execute.add_argument("--decision-id", required=True)
    execute.add_argument("--expected-decision-sha256", required=True)
    execute.add_argument("--actor", required=True)

    inspect = commands.add_parser(
        "inspect", help="Inspect immutable decision and exact result evidence."
    )
    inspect.add_argument("--decision-id", required=True)
    return parser.parse_args()


def _preview_args(args: argparse.Namespace) -> dict[str, object]:
    return {
        "action": args.action,
        "support_structure_id": args.support_id,
        "mounted_asset_type": args.asset_type,
        "mounted_asset_id": args.asset_id,
        "mount_role": args.mount_role,
        "sequence": args.sequence,
        "existing_mount_id": args.existing_mount_id,
        "reason": args.reason,
        "proposed_by": args.actor,
    }


def main() -> int:
    args = parse_args()
    try:
        with SessionLocal() as db:
            if args.command == "preview":
                output = preview_mount_decision(db, **_preview_args(args)).to_dict()
            elif args.command == "propose":
                row = propose_mount_decision(
                    db,
                    expected_decision_sha256=args.expected_decision_sha256,
                    **_preview_args(args),
                )
                output = inspect_mount_decision(db, row.id)
            elif args.command in {"approve", "decline"}:
                row = review_mount_decision(
                    db,
                    args.decision_id,
                    action=args.command,
                    reviewed_by=args.actor,
                    review_notes=args.notes,
                    expected_decision_sha256=args.expected_decision_sha256,
                )
                output = inspect_mount_decision(db, row.id)
            elif args.command == "execute":
                row = execute_mount_decision(
                    db,
                    args.decision_id,
                    executed_by=args.actor,
                    expected_decision_sha256=args.expected_decision_sha256,
                )
                output = inspect_mount_decision(db, row.id)
            else:
                output = inspect_mount_decision(db, args.decision_id)
    except FiberSupportStructureError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
