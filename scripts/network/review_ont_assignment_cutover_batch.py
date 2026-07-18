"""Preview, propose, and independently review immutable ONT cutover batches."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.network.ont_assignment_cutover_batches import (  # noqa: E402
    OntAssignmentCutoverBatchBlocked,
    OntAssignmentCutoverBatchError,
    inspect_ont_assignment_cutover_batch,
    preview_ont_assignment_cutover_batch,
    propose_ont_assignment_cutover_batch,
    review_ont_assignment_cutover_batch,
)


def _proposal_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--items-json",
        required=True,
        type=Path,
        help="Path to a JSON array of exact assignment actions and target IDs.",
    )
    parser.add_argument("--expected-report-sha256", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--source-name", default="operator")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bind operator-selected ONT identity repairs to an exhaustive audit, "
            "then independently approve or decline the immutable manifest. "
            "Approved repairs are executed individually through the identity owner."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    preview = commands.add_parser("preview", help="Resolve a manifest without writes.")
    _proposal_arguments(preview)

    propose = commands.add_parser(
        "propose", help="Persist a manifest and exact delegated proposals."
    )
    _proposal_arguments(propose)

    approve = commands.add_parser(
        "approve", help="Independently approve every exact proposal atomically."
    )
    approve.add_argument("--batch-id", required=True)
    approve.add_argument("--expected-manifest-sha256", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--notes", required=True)

    decline = commands.add_parser(
        "decline", help="Decline every exact proposal and preserve the evidence."
    )
    decline.add_argument("--batch-id", required=True)
    decline.add_argument("--expected-manifest-sha256", required=True)
    decline.add_argument("--actor", required=True)
    decline.add_argument("--notes", required=True)

    inspect = commands.add_parser("inspect", help="Print a stored batch and review.")
    inspect.add_argument("--batch-id", required=True)
    return parser.parse_args()


def _items(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise OntAssignmentCutoverBatchError(
            f"could not load items JSON: {exc}"
        ) from exc


def main() -> int:
    args = parse_args()
    try:
        with SessionLocal() as db:
            if args.command in {"preview", "propose"}:
                kwargs = {
                    "expected_report_sha256": args.expected_report_sha256,
                    "items": _items(args.items_json),
                    "proposed_by": args.actor,
                    "reason": args.reason,
                    "source_name": args.source_name,
                }
                if args.command == "preview":
                    result = preview_ont_assignment_cutover_batch(
                        db, **kwargs
                    ).to_dict()
                else:
                    result = propose_ont_assignment_cutover_batch(
                        db, **kwargs
                    ).to_dict()
            elif args.command in {"approve", "decline"}:
                result = review_ont_assignment_cutover_batch(
                    db,
                    args.batch_id,
                    expected_manifest_sha256=args.expected_manifest_sha256,
                    action=args.command,
                    reviewed_by=args.actor,
                    review_notes=args.notes,
                ).to_dict()
            else:
                result = inspect_ont_assignment_cutover_batch(db, args.batch_id)
    except OntAssignmentCutoverBatchBlocked as exc:
        print(json.dumps(exc.preview.to_dict(), indent=2, sort_keys=True))
        return 2
    except OntAssignmentCutoverBatchError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
