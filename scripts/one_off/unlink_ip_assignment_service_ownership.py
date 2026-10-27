#!/usr/bin/env python
"""Preview or apply the rollback inverse of one assignment service link.

This undoes a link made by the ownership/lifecycle repairs: it clears only
``IPAssignment.subscription_id`` (and the IPv4 primary marker). It never
deactivates or releases the address. The default is dry-run.
"""

from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID, uuid4

from app.db import SessionLocal
from app.services.ip_assignment_lifecycle import (
    UnlinkIPAssignmentServiceOwnershipCommand,
    preview_ip_assignment_service_unlink,
    unlink_ip_assignment_service_ownership,
)
from app.services.owner_commands import CommandContext


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assignment-id", required=True, type=_uuid)
    parser.add_argument("--expected-subscription-id", required=True, type=_uuid)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    return parser


def _require_apply_argument(
    parser: argparse.ArgumentParser,
    value: str | None,
    flag: str,
) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        parser.error(f"{flag} is required with --apply")
    return normalized


def main() -> int:
    parser = _parser()
    args = parser.parse_args()

    with SessionLocal() as session:
        preview = preview_ip_assignment_service_unlink(
            session,
            assignment_id=args.assignment_id,
            expected_subscription_id=args.expected_subscription_id,
        )
        print(
            json.dumps(
                {
                    "assignment_id": str(preview.assignment_id),
                    "subscriber_id": (
                        str(preview.subscriber_id)
                        if preview.subscriber_id is not None
                        else None
                    ),
                    "current_subscription_id": (
                        str(preview.current_subscription_id)
                        if preview.current_subscription_id is not None
                        else None
                    ),
                    "expected_subscription_id": str(preview.expected_subscription_id),
                    "ip_version": preview.ip_version,
                    "address_id": (
                        str(preview.address_id)
                        if preview.address_id is not None
                        else None
                    ),
                    "address": preview.address,
                    "is_primary": preview.is_primary,
                    "decision": preview.decision.value,
                    "applicable": preview.applicable,
                    "repair_fingerprint": preview.fingerprint,
                },
                indent=2,
                sort_keys=True,
            )
        )
        if not args.apply:
            print("DRY RUN — no changes written.")
            return 0
        if not preview.applicable:
            parser.error(f"current preview is not applicable: {preview.decision.value}")

        fingerprint = _require_apply_argument(parser, args.fingerprint, "--fingerprint")
        if fingerprint != preview.fingerprint:
            parser.error("--fingerprint does not match the current repair preview")
        idempotency_key = _require_apply_argument(
            parser, args.idempotency_key, "--idempotency-key"
        )
        actor = _require_apply_argument(parser, args.actor, "--actor")
        reason = _require_apply_argument(parser, args.reason, "--reason")
        session.rollback()
        outcome = unlink_ip_assignment_service_ownership(
            session,
            UnlinkIPAssignmentServiceOwnershipCommand(
                context=CommandContext.system(
                    command_id=uuid4(),
                    actor=actor,
                    scope="ipam_service_ownership_unlink",
                    reason=reason,
                    idempotency_key=idempotency_key,
                ),
                assignment_id=args.assignment_id,
                expected_subscription_id=args.expected_subscription_id,
                preview_fingerprint=fingerprint,
            ),
        )
        print(
            json.dumps(
                {
                    "assignment_id": str(outcome.assignment_id),
                    "previous_subscription_id": (
                        str(outcome.previous_subscription_id)
                        if outcome.previous_subscription_id is not None
                        else None
                    ),
                    "preview_fingerprint": outcome.preview_fingerprint,
                    "replayed": outcome.replayed,
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
