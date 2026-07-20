"""Preview and execute independently reviewed ONT identity repairs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.network.ont_assignment_identity import (  # noqa: E402
    OntAssignmentIdentityError,
    approve_assignment_identity_repair,
    assignment_identity_decision_to_dict,
    decline_assignment_identity_repair,
    execute_assignment_identity_repair,
    preview_assignment_identity_repair,
    propose_assignment_identity_repair,
)


def _repair_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--action", choices=("canonicalize", "deactivate"), required=True
    )
    parser.add_argument("--primary-assignment-id", required=True)
    parser.add_argument("--target-subscription-id")
    parser.add_argument("--target-pon-port-id")
    parser.add_argument("--target-olt-id")
    parser.add_argument(
        "--duplicate-assignment-id",
        action="append",
        default=[],
        help="Repeat for every exact active ONT or subscription conflict.",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preview, independently review, and execute exact ONT assignment "
            "identity repairs. Subscriber, address, and name inference are forbidden."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    preview = commands.add_parser("preview", help="Validate without writing.")
    _repair_arguments(preview)

    propose = commands.add_parser("propose", help="Record an exact proposal.")
    _repair_arguments(propose)
    propose.add_argument("--actor", required=True)
    propose.add_argument("--reason", required=True)

    approve = commands.add_parser("approve", help="Independently approve a proposal.")
    approve.add_argument("--decision-id", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--notes", required=True)

    decline = commands.add_parser("decline", help="Decline and preserve the evidence.")
    decline.add_argument("--decision-id", required=True)
    decline.add_argument("--actor", required=True)
    decline.add_argument("--notes", required=True)

    execute = commands.add_parser(
        "execute", help="Revalidate and apply an independently approved repair."
    )
    execute.add_argument("--decision-id", required=True)
    execute.add_argument("--actor", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with SessionLocal() as db:
            if args.command == "preview":
                result = preview_assignment_identity_repair(
                    db,
                    args.action,
                    args.primary_assignment_id,
                    target_subscription_id=args.target_subscription_id,
                    target_pon_port_id=args.target_pon_port_id,
                    target_olt_id=args.target_olt_id,
                    duplicate_assignment_ids=args.duplicate_assignment_id,
                ).to_dict()
            elif args.command == "propose":
                result = assignment_identity_decision_to_dict(
                    propose_assignment_identity_repair(
                        db,
                        args.action,
                        args.primary_assignment_id,
                        proposed_by=args.actor,
                        reason=args.reason,
                        target_subscription_id=args.target_subscription_id,
                        target_pon_port_id=args.target_pon_port_id,
                        target_olt_id=args.target_olt_id,
                        duplicate_assignment_ids=args.duplicate_assignment_id,
                    )
                )
            elif args.command == "approve":
                result = assignment_identity_decision_to_dict(
                    approve_assignment_identity_repair(
                        db,
                        args.decision_id,
                        reviewed_by=args.actor,
                        review_notes=args.notes,
                    )
                )
            elif args.command == "decline":
                result = assignment_identity_decision_to_dict(
                    decline_assignment_identity_repair(
                        db,
                        args.decision_id,
                        reviewed_by=args.actor,
                        review_notes=args.notes,
                    )
                )
            else:
                result = assignment_identity_decision_to_dict(
                    execute_assignment_identity_repair(
                        db, args.decision_id, executed_by=args.actor
                    )
                )
    except OntAssignmentIdentityError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
