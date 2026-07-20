"""Preview and attest post-execution ONT cleanup evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.network.ont_assignment_cutover_verification import (  # noqa: E402
    OntAssignmentCutoverVerificationBlocked,
    OntAssignmentCutoverVerificationError,
    attest_ont_assignment_cutover_verification,
    inspect_ont_assignment_cutover_verifications,
    preview_ont_assignment_cutover_verification,
)


def _verification_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--notes", required=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bind terminal identity-decision results to a fresh exhaustive ONT "
            "assignment audit. This command cannot execute repairs or enable "
            "constraints."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    preview = commands.add_parser(
        "preview", help="Build exact verification evidence without writing."
    )
    _verification_arguments(preview)

    attest = commands.add_parser(
        "attest", help="Persist an exact terminal-result verification snapshot."
    )
    _verification_arguments(attest)
    attest.add_argument("--expected-evidence-sha256", required=True)

    inspect = commands.add_parser(
        "inspect", help="Print stored verification attestations for one batch."
    )
    inspect.add_argument("--batch-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with SessionLocal() as db:
            if args.command == "preview":
                result = preview_ont_assignment_cutover_verification(
                    db,
                    args.batch_id,
                    expected_manifest_sha256=args.expected_manifest_sha256,
                    verified_by=args.actor,
                    verification_notes=args.notes,
                ).to_dict()
            elif args.command == "attest":
                result = attest_ont_assignment_cutover_verification(
                    db,
                    args.batch_id,
                    expected_manifest_sha256=args.expected_manifest_sha256,
                    expected_evidence_sha256=args.expected_evidence_sha256,
                    verified_by=args.actor,
                    verification_notes=args.notes,
                ).to_dict()
            else:
                result = inspect_ont_assignment_cutover_verifications(db, args.batch_id)
    except OntAssignmentCutoverVerificationBlocked as exc:
        print(json.dumps(exc.preview.to_dict(), indent=2, sort_keys=True))
        return 2
    except OntAssignmentCutoverVerificationError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
