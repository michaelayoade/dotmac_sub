#!/usr/bin/env python
"""Preview or apply the release of ONE unbound IPv4 assignment (N9 exception).

N9 is link-don't-release. This command is the named exception for an active
assignment that no service owns and that cannot be linked. You must name the
eligibility class you are invoking; the preview refuses unless the evidence
proves it. It retires only the ``IPAssignment`` row: the address, pool, served
columns and RADIUS are untouched. The default is dry-run.
"""

from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID, uuid4

from app.db import SessionLocal
from app.services.ip_assignment_lifecycle import (
    ReleaseUnboundIPAssignmentCommand,
    UnboundIPv4ReleaseClass,
    preview_unbound_ip_assignment_release,
    release_unbound_ip_assignment,
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
    parser.add_argument(
        "--eligibility-class",
        required=True,
        choices=[value.value for value in UnboundIPv4ReleaseClass],
        help="The ruled exception class this release invokes.",
    )
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
    eligibility_class = UnboundIPv4ReleaseClass(args.eligibility_class)

    with SessionLocal() as session:
        preview = preview_unbound_ip_assignment_release(
            session,
            assignment_id=args.assignment_id,
            eligibility_class=eligibility_class,
        )
        print(
            json.dumps(
                {
                    "assignment_id": str(preview.assignment_id),
                    "eligibility_class": preview.eligibility_class.value,
                    "subscriber_id": (
                        str(preview.subscriber_id)
                        if preview.subscriber_id is not None
                        else None
                    ),
                    "address_id": (
                        str(preview.address_id)
                        if preview.address_id is not None
                        else None
                    ),
                    "address": preview.address,
                    "is_active": preview.is_active,
                    "is_primary": preview.is_primary,
                    "current_subscription_id": (
                        str(preview.current_subscription_id)
                        if preview.current_subscription_id is not None
                        else None
                    ),
                    "non_terminal_services": [
                        {
                            "subscription_id": str(item.subscription_id),
                            "status": item.status,
                            "column_matches_address": item.column_matches_address,
                            "has_exact_active_ipv4": item.has_exact_active_ipv4,
                        }
                        for item in preview.services
                    ],
                    "live_session": preview.live_session,
                    "column_holder_subscription_ids": [
                        str(value) for value in preview.column_holder_subscription_ids
                    ],
                    "address_is_network_or_broadcast": (
                        preview.address_is_network_or_broadcast
                    ),
                    "address_is_protected": preview.address_is_protected,
                    "address_is_device_host": preview.address_is_device_host,
                    "address_in_routed_block": preview.address_in_routed_block,
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
            parser.error("--fingerprint does not match the current release preview")
        idempotency_key = _require_apply_argument(
            parser, args.idempotency_key, "--idempotency-key"
        )
        actor = _require_apply_argument(parser, args.actor, "--actor")
        reason = _require_apply_argument(parser, args.reason, "--reason")
        session.rollback()
        outcome = release_unbound_ip_assignment(
            session,
            ReleaseUnboundIPAssignmentCommand(
                context=CommandContext.system(
                    command_id=uuid4(),
                    actor=actor,
                    scope="ipam_unbound_assignment_release",
                    reason=reason,
                    idempotency_key=idempotency_key,
                ),
                assignment_id=args.assignment_id,
                eligibility_class=eligibility_class,
                preview_fingerprint=fingerprint,
            ),
        )
        print(
            json.dumps(
                {
                    "assignment_id": str(outcome.assignment_id),
                    "eligibility_class": outcome.eligibility_class.value,
                    "released_count": outcome.released_count,
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
