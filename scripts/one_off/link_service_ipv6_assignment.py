#!/usr/bin/env python
"""Preview or apply one reviewed exact-service IPv6 assignment link.

This command changes only ``IPAssignment`` rows. It does not change the served
subscription IPv6, external RADIUS, or live sessions. The default is dry-run.
"""

from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID, uuid4

from app.db import SessionLocal
from app.services.ip_assignment_lifecycle import (
    LinkServiceIPv6AssignmentCommand,
    link_service_ipv6_assignment,
    preview_service_ipv6_assignment_link,
)
from app.services.owner_commands import CommandContext


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription-id", required=True, type=_uuid)
    parser.add_argument("--desired-address-id", required=True, type=_uuid)
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
        preview = preview_service_ipv6_assignment_link(
            session,
            subscription_id=args.subscription_id,
            desired_address_id=args.desired_address_id,
        )
        print(
            json.dumps(
                {
                    "subscription_id": str(preview.subscription_id),
                    "subscriber_id": (
                        str(preview.subscriber_id)
                        if preview.subscriber_id is not None
                        else None
                    ),
                    "desired_address_id": str(preview.desired_address_id),
                    "desired_address": preview.desired_address,
                    "active_assignments": [
                        {
                            "assignment_id": str(item.assignment_id),
                            "subscription_id": (
                                str(item.subscription_id)
                                if item.subscription_id is not None
                                else None
                            ),
                            "address_id": str(item.address_id),
                            "address": item.address,
                        }
                        for item in preview.active_assignments
                    ],
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
        outcome = link_service_ipv6_assignment(
            session,
            LinkServiceIPv6AssignmentCommand(
                context=CommandContext.system(
                    command_id=uuid4(),
                    actor=actor,
                    scope="ip_assignment_lifecycle_repair",
                    reason=reason,
                    idempotency_key=idempotency_key,
                ),
                subscription_id=args.subscription_id,
                desired_address_id=args.desired_address_id,
                preview_fingerprint=fingerprint,
            ),
        )
        print(
            json.dumps(
                {
                    "subscription_id": str(outcome.subscription_id),
                    "desired_assignment_id": (
                        str(outcome.desired_assignment_id)
                        if outcome.desired_assignment_id is not None
                        else None
                    ),
                    "linked_count": outcome.linked_count,
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
