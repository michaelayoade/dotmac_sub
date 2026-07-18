#!/usr/bin/env python
"""Preview or confirm one exact legacy prepaid-payment cycle repair."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.billing import payments
from app.services.db_session_adapter import db_session_adapter


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical-payment-id", required=True)
    parser.add_argument("--historical-allocation-id", required=True)
    parser.add_argument("--historical-invoice-id", required=True)
    parser.add_argument("--historical-debit-ledger-entry-id", required=True)
    parser.add_argument("--renewal-payment-id", required=True)
    parser.add_argument("--draft-invoice-id", required=True)
    parser.add_argument("--subscription-id", required=True)
    parser.add_argument(
        "--idempotency-key",
        required=True,
        help="Stable 16-80 character key; required unchanged for confirmation.",
    )
    parser.add_argument(
        "--reason", help="Reviewed evidence reason; required with --apply."
    )
    parser.add_argument("--confirm-fingerprint")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Confirm the exact preview. Without this flag no rows are changed.",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    selected = {
        "historical_payment_id": args.historical_payment_id,
        "historical_allocation_id": args.historical_allocation_id,
        "historical_invoice_id": args.historical_invoice_id,
        "historical_debit_ledger_entry_id": (args.historical_debit_ledger_entry_id),
        "renewal_payment_id": args.renewal_payment_id,
        "draft_invoice_id": args.draft_invoice_id,
        "subscription_id": args.subscription_id,
    }
    session = db_session_adapter.create_session()
    try:
        if not args.apply:
            preview = payments.preview_prepaid_legacy_cycle_repair(session, **selected)
            print(json.dumps(asdict(preview), default=str, indent=2, sort_keys=True))
            return 0
        if not args.confirm_fingerprint:
            raise SystemExit("--confirm-fingerprint is required with --apply")
        if not args.reason:
            raise SystemExit("--reason is required with --apply")
        result = payments.confirm_prepaid_legacy_cycle_repair(
            session,
            **selected,
            preview_fingerprint=args.confirm_fingerprint,
            idempotency_key=args.idempotency_key,
            reason=args.reason,
        )
        access = payments.recheck_prepaid_application_access(
            session, str(result.renewal_application.id)
        )
        print(
            json.dumps(
                {
                    "historical_application_id": str(result.historical_application.id),
                    "renewal_application_id": str(result.renewal_application.id),
                    "renewal_invoice_id": str(
                        result.renewal_application.payment_allocation.invoice_id
                    ),
                    "renewal_payment_allocation_id": str(
                        result.renewal_application.payment_allocation_id
                    ),
                    "idempotent_replay": result.idempotent_replay,
                    "access_recheck_status": access.access_recheck_status,
                    "access_recheck_error": access.access_recheck_error,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
