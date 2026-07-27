#!/usr/bin/env python3
"""Plan or apply repair of rejected receipts with submitted deposit intents.

Dry-run is the default. Apply requires the exact fingerprint from a fresh
unlimited dry-run plus a named production target, attributable actor and
operator reason. Output contains only operational identifiers and amounts.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

from app.db import SessionLocal
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.payment_proofs import (
    REPAIR_SCOPE,
    RepairRejectedIntentCommand,
    preview_rejected_deposit_intent_repairs,
    repair_rejected_deposit_intents,
)


def _preview_payload(preview, *, mode: str) -> dict[str, object]:
    counts = Counter(item.classification.value for item in preview.candidates)
    return {
        "mode": mode,
        "observed_at": preview.observed_at.isoformat(),
        "preview_fingerprint": preview.fingerprint,
        "candidate_count": len(preview.candidates),
        "eligible_count": len(preview.repairs),
        "classification_counts": dict(sorted(counts.items())),
        "candidates": [
            {
                "intent_id": str(item.intent_id),
                "proof_id": str(item.proof_id) if item.proof_id else None,
                "account_id": str(item.account_id) if item.account_id else None,
                "reference": item.reference,
                "amount": f"{item.amount:.2f}",
                "currency": item.currency,
                "classification": item.classification.value,
            }
            for item in preview.candidates
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit dry-run inspection only; not allowed with --apply.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Omit per-record identifiers from the dry-run output.",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-fingerprint")
    parser.add_argument("--target")
    parser.add_argument("--actor-id")
    parser.add_argument("--reason")
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.apply and args.limit is not None:
        parser.error("--limit is not allowed with --apply")
    if args.apply and not all(
        (args.confirm_fingerprint, args.target, args.actor_id, args.reason)
    ):
        parser.error(
            "--apply requires --confirm-fingerprint, --target, --actor-id, and --reason"
        )

    with SessionLocal() as planner_db:
        preview = preview_rejected_deposit_intent_repairs(
            planner_db,
            limit=args.limit,
        )
    payload = _preview_payload(preview, mode="apply" if args.apply else "dry-run")
    if args.summary_only:
        payload.pop("candidates", None)

    if not args.apply:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.confirm_fingerprint.strip().lower() != preview.fingerprint:
        parser.error("--confirm-fingerprint does not match the fresh unlimited dry-run")
    if not preview.repairs:
        parser.error("the fresh dry-run has no eligible repair items")

    with SessionLocal() as command_db:
        db_session_adapter.release_read_transaction(command_db)
        outcome = repair_rejected_deposit_intents(
            command_db,
            RepairRejectedIntentCommand(
                context=CommandContext.system(
                    actor=args.actor_id,
                    scope=REPAIR_SCOPE,
                    reason=args.reason,
                    idempotency_key=preview.fingerprint,
                ),
                preview_fingerprint=preview.fingerprint,
                target=args.target,
                repairs=preview.repairs,
            ),
        )
    payload.update(
        {
            "applied_count": outcome.applied_count,
            "already_applied": outcome.already_applied,
            "target": args.target,
        }
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
