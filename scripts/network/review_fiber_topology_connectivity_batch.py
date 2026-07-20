"""Review exact staged-path connectivity manifests without geometry inference."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.network.fiber_topology_connectivity_review import (  # noqa: E402
    FiberTopologyConnectivityProposalBatchBlocked,
    FiberTopologyConnectivityReviewError,
    attest_connectivity_batch,
    execute_connectivity_batch,
    inspect_connectivity_batch,
    preview_connectivity_proposal_batch,
    propose_connectivity_batch,
    reconcile_connectivity_batch,
)


def _manifest_items(path: str) -> list[dict]:
    payload = json.loads(Path(path).read_text())
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise FiberTopologyConnectivityReviewError(
            "manifest must be a JSON array or an object containing items"
        )
    return items


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preview, propose, independently attest, and run bounded fiber "
            "connectivity batches. Every create/link row requires exact staged "
            "content plus explicit endpoint IDs; no endpoint is inferred from geometry."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    for name in ("preview", "propose"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", required=True)
        command.add_argument("--actor", required=True)
        command.add_argument("--reason", required=True)
        command.add_argument("--source-name", default="operator-manifest")

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--batch-id", required=True)

    for name in ("approve", "decline"):
        command = commands.add_parser(name)
        command.add_argument("--batch-id", required=True)
        command.add_argument("--expected-manifest-sha256", required=True)
        command.add_argument("--actor", required=True)
        command.add_argument("--notes", required=True)

    for name in ("execute", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("--batch-id", required=True)
        command.add_argument("--expected-manifest-sha256", required=True)
        command.add_argument("--actor", required=True)
        command.add_argument("--limit", type=int, default=50)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with SessionLocal() as db:
            if args.command in {"preview", "propose"}:
                items = _manifest_items(args.manifest)
                if args.command == "preview":
                    result = preview_connectivity_proposal_batch(
                        db,
                        items,
                        proposed_by=args.actor,
                        reason=args.reason,
                        source_name=args.source_name,
                    ).to_dict()
                else:
                    result = propose_connectivity_batch(
                        db,
                        items,
                        proposed_by=args.actor,
                        reason=args.reason,
                        source_name=args.source_name,
                    ).to_dict()
            elif args.command == "inspect":
                result = inspect_connectivity_batch(db, args.batch_id)
            elif args.command in {"approve", "decline"}:
                result = attest_connectivity_batch(
                    db,
                    args.batch_id,
                    expected_manifest_sha256=args.expected_manifest_sha256,
                    action=args.command,
                    reviewed_by=args.actor,
                    review_notes=args.notes,
                ).to_dict()
            elif args.command == "execute":
                result = execute_connectivity_batch(
                    db,
                    args.batch_id,
                    expected_manifest_sha256=args.expected_manifest_sha256,
                    executed_by=args.actor,
                    limit=args.limit,
                ).to_dict()
            else:
                result = reconcile_connectivity_batch(
                    db,
                    args.batch_id,
                    expected_manifest_sha256=args.expected_manifest_sha256,
                    finalized_by=args.actor,
                    limit=args.limit,
                ).to_dict()
    except FiberTopologyConnectivityProposalBatchBlocked as exc:
        print(json.dumps(exc.preview.to_dict(), indent=2, sort_keys=True))
        return 2
    except (FiberTopologyConnectivityReviewError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
