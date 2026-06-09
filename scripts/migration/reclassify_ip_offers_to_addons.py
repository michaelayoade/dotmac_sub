"""Reclassify standalone public-IP-block subscriptions into add-ons.

DRY-RUN by default: prints exactly what would move (and what can't), commits
nothing. Review the report, then re-run with --execute.

    python -m scripts.migration.reclassify_ip_offers_to_addons            # dry-run
    python -m scripts.migration.reclassify_ip_offers_to_addons --execute  # apply

Prerequisite: the IP add-ons must already exist
(python -m scripts.migration.import_addons_from_splynx --execute).
"""

from __future__ import annotations

import argparse
import json
import logging

from app.services.migrations.db_connections import dotmac_session
from app.services.migrations.reclassify_ip_offers import (
    apply_reclassification,
    build_reclassification_plan,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reclassify_ip_offers")


def _print_report(plan: dict) -> None:
    print("\n=== RECLASSIFICATION PLAN ===")
    print(json.dumps(plan["summary"], indent=2, default=str))

    skipped = [i for i in plan["items"] if i["decision"] == "skip"]
    if skipped:
        print(f"\n--- {len(skipped)} cannot move (need attention) ---")
        for i in skipped[:50]:
            print(
                f"  {i['subscriber_name']}  {i['ip_offer_name']}"
                f"  reason={i['reason']}  ip={i['ipv4_address']}"
            )

    reclass = [i for i in plan["items"] if i["decision"] == "reclassify"]
    print(f"\n--- {len(reclass)} would reclassify (first 20) ---")
    for i in reclass[:20]:
        flag = " ⚠ has-IP" if i.get("ipv4_address") else ""
        amb = " ⚠ multi-plan" if i.get("main_candidates", 1) > 1 else ""
        print(
            f"  {i['subscriber_name']}: /{i['prefix']} → main="
            f"{i['target_main_subscription_id']} addon={i['target_addon_id']}"
            f"{flag}{amb}"
        )


def run(*, execute: bool) -> None:
    with dotmac_session() as db:
        if execute:
            result = apply_reclassification(db, commit=True)
            print("\n=== APPLIED ===")
            print(json.dumps(result, indent=2, default=str))
        else:
            plan = build_reclassification_plan(db)
            _print_report(plan)
            print("\n(DRY-RUN — nothing committed. Re-run with --execute to apply.)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--execute",
        action="store_true",
        help="Apply the reclassification (default is a read-only dry-run).",
    )
    args = p.parse_args()
    run(execute=args.execute)
