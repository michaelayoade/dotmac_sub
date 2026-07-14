#!/usr/bin/env python
"""Report exactly what the prepaid balance sweep would do, without writes.

Examples (inside the app container):

    python scripts/one_off/plan_prepaid_balance_sweep.py
    python scripts/one_off/plan_prepaid_balance_sweep.py --limit 100 --details
    python scripts/one_off/plan_prepaid_balance_sweep.py --out /tmp/prepaid-plan.json
    python scripts/one_off/plan_prepaid_balance_sweep.py \
      --funding-snapshot /tmp/cutover-funding.json \
      --activation-at 2026-07-20T08:00:00+01:00

Funding snapshot shape (amounts are decimal strings; timestamp needs a zone):

    {
      "source": "splynx-cutover-plus-native-events:prod-2026-07-14",
      "captured_at": "2026-07-14T12:08:25Z",
      "accounts": [{
        "account_id": "...",
        "available_balance": "123.45",
        "required_balance": "5000.00"
      }]
    }

This command has no execute mode. Enabling the production control remains a
separate, explicit operator decision after the report is reviewed.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.db import SessionLocal
from app.services.access_resolution import PrepaidFundingDecision
from app.services.prepaid_enforcement_planner import (
    PrepaidFundingSnapshot,
    plan_prepaid_enforcement,
)

SAMPLE_SIZE = 20


def _json_default(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _parse_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed


def _parse_money(value: object, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal amount") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal amount")
    return parsed


def _load_funding_snapshot(path: str) -> PrepaidFundingSnapshot:
    """Load explicit reconstructed funding facts; never infer missing rows."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("funding snapshot must be a JSON object")
    rows = payload.get("accounts")
    if not isinstance(rows, list) or not rows:
        raise ValueError("funding snapshot accounts must be a non-empty list")

    decisions: list[PrepaidFundingDecision] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"accounts[{index}] must be a JSON object")
        account_id = row.get("account_id")
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError(f"accounts[{index}].account_id must be a UUID string")
        decisions.append(
            PrepaidFundingDecision(
                account_id=account_id,
                available_balance=_parse_money(
                    row.get("available_balance"),
                    field=f"accounts[{index}].available_balance",
                ),
                required_balance=_parse_money(
                    row.get("required_balance"),
                    field=f"accounts[{index}].required_balance",
                ),
            )
        )

    return PrepaidFundingSnapshot(
        captured_at=_parse_datetime(payload.get("captured_at"), field="captured_at"),
        source=str(payload.get("source") or ""),
        decisions=tuple(decisions),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--account-id",
        action="append",
        default=[],
        help="Repeatable subscriber UUID. Omit to inspect the full candidate cohort.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the deterministic account cohort for a staged review.",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Print every planned account instead of a 20-row sample.",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default=None,
        help="Write the complete JSON plan to PATH.",
    )
    parser.add_argument(
        "--funding-snapshot",
        metavar="PATH",
        default=None,
        help=(
            "Use reconstructed funding facts from JSON instead of the local "
            "financial resolver. Requires source, captured_at, and accounts[]."
        ),
    )
    parser.add_argument(
        "--activation-at",
        default=None,
        help=(
            "Preview a proposed ISO 8601 activation time. This does not persist "
            "the setting or enable enforcement."
        ),
    )
    args = parser.parse_args()

    funding_snapshot = (
        _load_funding_snapshot(args.funding_snapshot) if args.funding_snapshot else None
    )
    activation_at = (
        _parse_datetime(args.activation_at, field="activation_at")
        if args.activation_at
        else None
    )

    db = SessionLocal()
    try:
        plan = plan_prepaid_enforcement(
            db,
            account_ids=args.account_id or None,
            limit=args.limit,
            funding_snapshot=funding_snapshot,
            activation_at=activation_at,
        )
        summary = plan.to_dict(include_items=False)
        print("=== prepaid balance enforcement dry run ===")
        print(json.dumps(summary, indent=2, sort_keys=True, default=_json_default))

        rows = [item.to_dict() for item in plan.items]
        displayed = rows if args.details else rows[:SAMPLE_SIZE]
        if displayed:
            print(f"\n--- accounts (showing {len(displayed)} of {len(rows)}) ---")
            print(
                json.dumps(
                    displayed,
                    indent=2,
                    sort_keys=True,
                    default=_json_default,
                )
            )

        if args.out:
            with open(args.out, "w", encoding="utf-8") as output:
                json.dump(
                    plan.to_dict(),
                    output,
                    indent=2,
                    sort_keys=True,
                    default=_json_default,
                )
                output.write("\n")
            print(f"\nFull plan written: {args.out} ({len(rows)} accounts)")

        print(
            "\nDRY RUN ONLY - no timers, notices, service states, or sessions changed."
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
