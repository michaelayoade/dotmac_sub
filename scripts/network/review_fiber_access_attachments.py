"""Preview and execute reviewed electronic and splitter-cascade attachments."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.network.fiber_access_attachments import (  # noqa: E402
    FiberAccessAttachmentError,
    approve_access_attachment,
    attachment_decision_to_dict,
    decline_access_attachment,
    execute_access_attachment,
    preview_access_attachment,
    propose_access_attachment,
)


def _attachment_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--attachment-type",
        choices=("pon_input", "ont_output", "splitter_cascade"),
        required=True,
    )
    parser.add_argument("--action", choices=("attach", "detach"), required=True)
    parser.add_argument(
        "--subject-id",
        required=True,
        help=(
            "PON port UUID for pon_input, ONT UUID for ont_output, or upstream "
            "splitter output-port UUID for splitter_cascade."
        ),
    )
    parser.add_argument(
        "--splitter-port-id",
        help="Required for attach and forbidden for detach.",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preview, independently review, and execute exact fiber access "
            "attachments. Geometry and proximity are never accepted as links."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    preview = commands.add_parser("preview", help="Validate without writing.")
    _attachment_arguments(preview)

    propose = commands.add_parser("propose", help="Record an exact proposal.")
    _attachment_arguments(propose)
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
        "execute", help="Revalidate and apply the independently approved mutation."
    )
    execute.add_argument("--decision-id", required=True)
    execute.add_argument("--actor", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with SessionLocal() as db:
            if args.command == "preview":
                result = preview_access_attachment(
                    db,
                    args.attachment_type,
                    args.action,
                    args.subject_id,
                    args.splitter_port_id,
                ).to_dict()
            elif args.command == "propose":
                result = attachment_decision_to_dict(
                    propose_access_attachment(
                        db,
                        args.attachment_type,
                        args.action,
                        args.subject_id,
                        args.splitter_port_id,
                        proposed_by=args.actor,
                        reason=args.reason,
                    )
                )
            elif args.command == "approve":
                result = attachment_decision_to_dict(
                    approve_access_attachment(
                        db,
                        args.decision_id,
                        reviewed_by=args.actor,
                        review_notes=args.notes,
                    )
                )
            elif args.command == "decline":
                result = attachment_decision_to_dict(
                    decline_access_attachment(
                        db,
                        args.decision_id,
                        reviewed_by=args.actor,
                        review_notes=args.notes,
                    )
                )
            else:
                result = attachment_decision_to_dict(
                    execute_access_attachment(
                        db, args.decision_id, executed_by=args.actor
                    )
                )
    except FiberAccessAttachmentError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
