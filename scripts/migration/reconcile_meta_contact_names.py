#!/usr/bin/env python3
"""Preview or repair missing Facebook and Instagram Inbox names."""

from __future__ import annotations

import argparse
import json

from app.db import SessionLocal, finish_read_transaction
from app.services import team_inbox_maintenance
from app.services.integrations.meta_social_capability import fetch_contact_profile
from app.services.integrations.meta_social_contracts import MetaSocialChannel
from app.services.owner_commands import CommandContext


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest", default="")
    args = parser.parse_args()
    with SessionLocal() as db:
        preview = team_inbox_maintenance.preview_meta_profile_repairs(
            db, limit=args.limit
        )
        result: dict[str, object] = {
            "candidate_count": len(preview.candidates),
            "digest": preview.digest,
            "applied": 0,
            "unavailable": 0,
        }
        if not args.apply:
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if not args.expected_digest or args.expected_digest != preview.digest:
            result["error"] = "Preview digest is missing or changed; run preview again."
            print(json.dumps(result, indent=2, sort_keys=True))
            return 2
        finish_read_transaction(db)
        applied = 0
        unavailable = 0
        for candidate in preview.candidates:
            profile = fetch_contact_profile(
                db,
                channel=MetaSocialChannel(candidate.channel_type),
                contact_id=candidate.contact_address,
            )
            finish_read_transaction(db)
            if profile is None or not (profile.display_name or profile.username):
                unavailable += 1
                continue
            team_inbox_maintenance.apply_meta_profile_observation(
                db,
                team_inbox_maintenance.ApplyMetaProfileObservationCommand(
                    context=CommandContext.system(
                        actor="operator.meta_contact_name_repair",
                        scope="communications:team-inbox-maintenance",
                        reason="Repair missing Meta contact name from provider profile",
                        idempotency_key=f"meta-profile-repair:{candidate.conversation_id}",
                    ),
                    conversation_id=candidate.conversation_id,
                    expected_channel_type=candidate.channel_type,
                    expected_contact_address=candidate.contact_address,
                    display_name=profile.display_name or profile.username or "",
                    username=profile.username,
                    profile_pic=profile.profile_pic,
                ),
            )
            applied += 1
        result["applied"] = applied
        result["unavailable"] = unavailable
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
