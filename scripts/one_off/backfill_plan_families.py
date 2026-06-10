"""Backfill catalog_offers.plan_family from offer names.

plan_family was never populated, which makes the portal's family-scoped
upgrade filter a no-op (None == None matches every offer): an Unlimited
customer's change-plan list shows Dedicated and Homeflex plans too. The
families map mechanically from the naming convention:

    Unlimited*            -> unlimited
    Homeflex* / Home Flex* -> home_flex
    *Dedicated*           -> dedicated

Offers matching none of the patterns are left untouched and reported.

Usage:
    python -m scripts.one_off.backfill_plan_families --dry-run
    python -m scripts.one_off.backfill_plan_families --live
"""

from __future__ import annotations

import argparse
import re

from app.db import SessionLocal
from app.models.catalog import PLAN_FAMILY_VALUES, CatalogOffer

_PATTERNS = (
    (re.compile(r"\bunlimited\b", re.I), "unlimited"),
    (re.compile(r"\bhome\s*flex\b|\bhomeflex\b", re.I), "home_flex"),
    (re.compile(r"\bdedicated\b", re.I), "dedicated"),
)


def classify(name: str) -> str | None:
    for pattern, family in _PATTERNS:
        if pattern.search(name or ""):
            return family
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    stats = {"set": 0, "already_set": 0, "unmatched": 0}
    try:
        offers = db.query(CatalogOffer).filter(CatalogOffer.is_active.is_(True)).all()
        for offer in offers:
            if offer.plan_family:
                stats["already_set"] += 1
                continue
            family = classify(offer.name)
            if family is None:
                stats["unmatched"] += 1
                if offer.show_on_customer_portal:
                    print(f"unmatched (portal-visible!): {offer.name}")
                continue
            assert family in PLAN_FAMILY_VALUES
            print(f"{offer.name[:50]:52} -> {family}")
            if args.live:
                offer.plan_family = family
            stats["set"] += 1
        if args.live:
            db.commit()
    finally:
        db.close()
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}{stats}")


if __name__ == "__main__":
    main()
