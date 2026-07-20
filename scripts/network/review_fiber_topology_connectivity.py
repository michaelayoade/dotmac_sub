"""Review staged path connectivity without deriving edges from geometry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.network.fiber_topology_connectivity import (  # noqa: E402
    FiberTopologyConnectivityError,
    approve_connectivity_decision,
    connectivity_decision_to_dict,
    decline_connectivity_decision,
    execute_connectivity_decision,
    finalize_connectivity_decision,
    propose_connectivity_decision,
    reconcile_connectivity_change_requests,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Propose, independently review, and advance staged fiber paths with "
            "explicit canonical endpoints. This command emits pending fiber change "
            "requests and never approves canonical mutations."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    propose = commands.add_parser(
        "propose", help="Propose create, link, or reject for one staged path."
    )
    propose.add_argument("--feature-id", required=True)
    propose.add_argument(
        "--action", choices=("create", "link_existing", "reject"), required=True
    )
    propose.add_argument("--start-endpoint-type")
    propose.add_argument("--start-endpoint-ref-id")
    propose.add_argument("--end-endpoint-type")
    propose.add_argument("--end-endpoint-ref-id")
    propose.add_argument(
        "--segment-type",
        choices=("feeder", "distribution", "drop"),
        default="distribution",
    )
    propose.add_argument(
        "--cable-type",
        choices=(
            "single_mode",
            "multi_mode",
            "armored",
            "aerial",
            "underground",
            "direct_buried",
        ),
    )
    propose.add_argument("--fiber-count", type=int)
    propose.add_argument("--length-m", type=float)
    propose.add_argument("--target-segment-id")
    propose.add_argument("--actor", required=True)
    propose.add_argument("--reason", required=True)

    approve = commands.add_parser(
        "approve", help="Independently approve an exact connectivity decision."
    )
    approve.add_argument("--decision-id", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--notes", required=True)

    decline = commands.add_parser(
        "decline", help="Decline a proposed decision while preserving evidence."
    )
    decline.add_argument("--decision-id", required=True)
    decline.add_argument("--actor", required=True)
    decline.add_argument("--notes", required=True)

    execute = commands.add_parser(
        "execute",
        help="Resolve endpoints or link/reject an independently approved decision.",
    )
    execute.add_argument("--decision-id", required=True)
    execute.add_argument("--actor", required=True)

    finalize = commands.add_parser(
        "finalize", help="Project reviewed endpoint/segment request outcomes."
    )
    finalize.add_argument("--decision-id", required=True)
    finalize.add_argument("--actor", required=True)

    reconcile = commands.add_parser(
        "reconcile", help="Sweep pending connectivity change-request outcomes."
    )
    reconcile.add_argument("--actor", required=True)
    reconcile.add_argument("--limit", type=int, default=100)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with SessionLocal() as db:
            if args.command == "propose":
                result = connectivity_decision_to_dict(
                    propose_connectivity_decision(
                        db,
                        args.feature_id,
                        args.action,
                        proposed_by=args.actor,
                        reason=args.reason,
                        start_endpoint_type=args.start_endpoint_type,
                        start_endpoint_ref_id=args.start_endpoint_ref_id,
                        end_endpoint_type=args.end_endpoint_type,
                        end_endpoint_ref_id=args.end_endpoint_ref_id,
                        segment_type=args.segment_type,
                        cable_type=args.cable_type,
                        fiber_count=args.fiber_count,
                        length_m=args.length_m,
                        target_segment_id=args.target_segment_id,
                    )
                )
            elif args.command == "approve":
                result = connectivity_decision_to_dict(
                    approve_connectivity_decision(
                        db,
                        args.decision_id,
                        reviewed_by=args.actor,
                        review_notes=args.notes,
                    )
                )
            elif args.command == "decline":
                result = connectivity_decision_to_dict(
                    decline_connectivity_decision(
                        db,
                        args.decision_id,
                        reviewed_by=args.actor,
                        review_notes=args.notes,
                    )
                )
            elif args.command == "execute":
                result = connectivity_decision_to_dict(
                    execute_connectivity_decision(
                        db, args.decision_id, executed_by=args.actor
                    )
                )
            elif args.command == "finalize":
                result = connectivity_decision_to_dict(
                    finalize_connectivity_decision(
                        db, args.decision_id, finalized_by=args.actor
                    )
                )
            else:
                reconciliation = reconcile_connectivity_change_requests(
                    db, finalized_by=args.actor, limit=args.limit
                )
                result = reconciliation.to_dict()
                if reconciliation.errors:
                    print(json.dumps(result, indent=2, sort_keys=True))
                    return 2
    except FiberTopologyConnectivityError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
