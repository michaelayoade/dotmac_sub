#!/usr/bin/env python
"""Read-only recovery preview by default; apply one reviewed purchase or outage."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.system_user import SystemUser
from app.services.auth_dependencies import has_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.outage_compensation import (
    OUTAGE_APPROVAL_SCOPE,
    ApproveOutageCompensationCommand,
    AttestLegacyTimeCreditCommand,
    ReviewOutageCompensationCommand,
    approve_outage_compensation,
    attest_legacy_time_credit,
    preview_legacy_time_credit,
    preview_outage_compensation,
    review_outage_compensation,
)
from app.services.owner_commands import CommandContext
from app.services.prepaid_period_purchases import (
    PURCHASE_REPAIR_SCOPE,
    RetryPurchaseSettlementCommand,
    preview_purchase_recovery,
    retry_purchase_settlement,
)
from app.services.system_user_assignments import system_user_role_names


def _permission(db: Session, principal_id: UUID) -> bool:
    principal = db.get(SystemUser, principal_id)
    if principal is None or not principal.is_active:
        return False
    return has_permission(
        {
            "principal_id": str(principal_id),
            "principal_type": "system_user",
            "roles": set(system_user_role_names(db, principal_id)),
        },
        db,
        PURCHASE_REPAIR_SCOPE,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    entity = parser.add_mutually_exclusive_group(required=True)
    entity.add_argument("--purchase-id", type=UUID)
    entity.add_argument("--subscription-id", type=UUID)
    entity.add_argument("--legacy-extension-entry", type=UUID)
    parser.add_argument("--approve-outage", action="store_true")
    parser.add_argument("--credited-from", type=datetime.fromisoformat)
    parser.add_argument("--credited-until", type=datetime.fromisoformat)
    parser.add_argument("--review-decision-id", type=UUID)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor-system-user-id", type=UUID)
    parser.add_argument("--reason")
    args = parser.parse_args()
    if args.subscription_id and not args.review_decision_id:
        parser.error("Outage recovery requires --review-decision-id")
    if args.apply and not all(
        (args.fingerprint, args.idempotency_key, args.actor_system_user_id, args.reason)
    ):
        parser.error(
            "Apply requires the reviewed fingerprint, idempotency key, active staff ID, and reason"
        )
    if args.approve_outage and not args.subscription_id:
        parser.error("Outage approval requires the subscription and reviewed decision")
    if bool(args.credited_from) != bool(args.credited_until):
        parser.error("Specify both credited clock boundaries")
    if args.credited_from and (
        args.credited_from.tzinfo is None or args.credited_until.tzinfo is None
    ):
        parser.error("Credited clock boundaries require explicit timezone offsets")
    from app.services.outage_interval_algebra import TimeInterval

    credited_ranges = (
        (TimeInterval(args.credited_from, args.credited_until),)
        if args.credited_from
        else None
    )
    now = datetime.now(UTC)
    with db_session_adapter.owner_command_session() as db:
        if not args.apply:
            preview = (
                preview_legacy_time_credit(
                    db, args.legacy_extension_entry, ranges=credited_ranges
                )
                if args.legacy_extension_entry
                else preview_purchase_recovery(db, args.purchase_id)
                if args.purchase_id
                else preview_outage_compensation(
                    db,
                    subscription_id=args.subscription_id,
                    effective_at=now,
                    review_decision_id=args.review_decision_id,
                )
            )
            print(json.dumps(asdict(preview), default=str, sort_keys=True))
            return
        principal_id: UUID = args.actor_system_user_id
        granted = _permission(db, principal_id)
        db_session_adapter.release_read_transaction(db)
        context = CommandContext.system(
            actor=f"user:{principal_id}",
            scope=OUTAGE_APPROVAL_SCOPE
            if args.approve_outage
            else PURCHASE_REPAIR_SCOPE,
            reason=args.reason,
            idempotency_key=args.idempotency_key,
        )
        if args.legacy_extension_entry:
            preview = preview_legacy_time_credit(
                db, args.legacy_extension_entry, ranges=credited_ranges
            )
            db_session_adapter.release_read_transaction(db)
            result = attest_legacy_time_credit(
                db,
                AttestLegacyTimeCreditCommand(
                    entry_id=args.legacy_extension_entry,
                    ranges=preview.ranges,
                    expected_fingerprint=args.fingerprint,
                    actor_system_user_id=principal_id,
                ),
                context=context,
            )
        elif args.approve_outage:
            result = approve_outage_compensation(
                db,
                ApproveOutageCompensationCommand(
                    decision_id=args.review_decision_id,
                    expected_fingerprint=args.fingerprint,
                    actor_system_user_id=principal_id,
                    effective_at=now,
                ),
                context=context,
            )
        elif args.purchase_id:
            result = retry_purchase_settlement(
                db,
                RetryPurchaseSettlementCommand(
                    purchase_id=args.purchase_id,
                    expected_fingerprint=args.fingerprint,
                    effective_at=now,
                    permission_granted=granted,
                    actor_system_user_id=principal_id,
                ),
                context=context,
            )
        else:
            result = review_outage_compensation(
                db,
                ReviewOutageCompensationCommand(
                    subscription_id=args.subscription_id,
                    review_decision_id=args.review_decision_id,
                    expected_fingerprint=args.fingerprint,
                    effective_at=now,
                    permission_granted=granted,
                    actor_system_user_id=principal_id,
                ),
                context=context,
            )
        print(json.dumps(asdict(result), default=str, sort_keys=True))


if __name__ == "__main__":
    main()
