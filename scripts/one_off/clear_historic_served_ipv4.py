#!/usr/bin/env python
"""Preview or apply the clearing of ONE historic served-IPv4 display column.

W0 N5 column pass. NULLs ``Subscription.ipv4_address`` on a non-active service
whose exact active primary assignment does not equal it. Skips active services,
online services (the suspended address-list block reads this column),
test-connection grants and anything RADIUS still projects. Never touches
assignments, RADIUS or sessions. ``--restore`` is the inverse: it puts back the
value the latest clear recorded. The default is dry-run.
"""

from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID, uuid4

from app.db import SessionLocal
from app.services.ip_assignment_lifecycle import (
    ClearHistoricServedIPv4Command,
    RestoreClearedServedIPv4Command,
    clear_historic_served_ipv4,
    preview_cleared_served_ipv4_restore,
    preview_historic_served_ipv4_clear,
    restore_cleared_served_ipv4,
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
    parser.add_argument(
        "--restore",
        action="store_true",
        help="Inverse: restore the value the latest clear recorded.",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    return parser


def _require_apply_argument(
    parser: argparse.ArgumentParser, value: str | None, flag: str
) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        parser.error(f"{flag} is required with --apply")
    return normalized


def main() -> int:
    parser = _parser()
    args = parser.parse_args()

    with SessionLocal() as session:
        if args.restore:
            restore_preview = preview_cleared_served_ipv4_restore(
                session, subscription_id=args.subscription_id
            )
            payload = {
                "mode": "restore",
                "subscription_id": str(restore_preview.subscription_id),
                "current_served_ipv4": restore_preview.current_served_ipv4,
                "restore_ipv4": restore_preview.restore_ipv4,
                "live_use": restore_preview.live_use,
                "decision": restore_preview.decision.value,
                "applicable": restore_preview.applicable,
                "repair_fingerprint": restore_preview.fingerprint,
            }
            applicable = restore_preview.applicable
            fingerprint_now = restore_preview.fingerprint
            decision_now = restore_preview.decision.value
        else:
            clear_preview = preview_historic_served_ipv4_clear(
                session, subscription_id=args.subscription_id
            )
            payload = {
                "mode": "clear",
                "subscription_id": str(clear_preview.subscription_id),
                "subscriber_id": (
                    str(clear_preview.subscriber_id)
                    if clear_preview.subscriber_id is not None
                    else None
                ),
                "status": clear_preview.status,
                "served_ipv4": clear_preview.served_ipv4,
                "own_primary_address": clear_preview.own_primary_address,
                "own_exact_addresses": list(clear_preview.own_exact_addresses),
                "ip_held_by_active_assignment": (
                    clear_preview.ip_held_by_active_assignment
                ),
                "online_session": clear_preview.online_session,
                "open_accounting_session": clear_preview.open_accounting_session,
                "test_connection_access": clear_preview.test_connection_access,
                "radius_mode": clear_preview.radius_mode,
                "address_list_block_dependency": (
                    clear_preview.address_list_block_dependency
                ),
                "pending_link_key": clear_preview.pending_link_key,
                "decision": clear_preview.decision.value,
                "applicable": clear_preview.applicable,
                "repair_fingerprint": clear_preview.fingerprint,
            }
            applicable = clear_preview.applicable
            fingerprint_now = clear_preview.fingerprint
            decision_now = clear_preview.decision.value
        print(json.dumps(payload, indent=2, sort_keys=True))
        if not args.apply:
            print("DRY RUN — no changes written.")
            return 0
        if not applicable:
            parser.error(f"current preview is not applicable: {decision_now}")

        fingerprint = _require_apply_argument(parser, args.fingerprint, "--fingerprint")
        if fingerprint != fingerprint_now:
            parser.error("--fingerprint does not match the current preview")
        idempotency_key = _require_apply_argument(
            parser, args.idempotency_key, "--idempotency-key"
        )
        actor = _require_apply_argument(parser, args.actor, "--actor")
        reason = _require_apply_argument(parser, args.reason, "--reason")
        session.rollback()
        context = CommandContext.system(
            command_id=uuid4(),
            actor=actor,
            scope="ipam_historic_served_ipv4",
            reason=reason,
            idempotency_key=idempotency_key,
        )
        if args.restore:
            restored = restore_cleared_served_ipv4(
                session,
                RestoreClearedServedIPv4Command(
                    context=context,
                    subscription_id=args.subscription_id,
                    preview_fingerprint=fingerprint,
                ),
            )
            result = {
                "subscription_id": str(restored.subscription_id),
                "restored_ipv4": restored.restored_ipv4,
                "preview_fingerprint": restored.preview_fingerprint,
                "replayed": restored.replayed,
            }
        else:
            cleared = clear_historic_served_ipv4(
                session,
                ClearHistoricServedIPv4Command(
                    context=context,
                    subscription_id=args.subscription_id,
                    preview_fingerprint=fingerprint,
                ),
            )
            result = {
                "subscription_id": str(cleared.subscription_id),
                "cleared_ipv4": cleared.cleared_ipv4,
                "preview_fingerprint": cleared.preview_fingerprint,
                "replayed": cleared.replayed,
            }
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
