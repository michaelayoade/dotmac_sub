#!/usr/bin/env python3
"""Idempotently converge legacy platform branding into the Branding SOT."""

from __future__ import annotations

import argparse
import json

from app.db import SessionLocal
from app.services.brand_profiles import sync_platform_brand_from_legacy_settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist the backfill")
    args = parser.parse_args()

    with SessionLocal() as db:
        profile = sync_platform_brand_from_legacy_settings(db)
        payload = {
            "mode": "apply" if args.apply else "dry_run",
            "profile_id": str(profile.id),
            "scope_type": profile.scope_type,
            "product_name": profile.product_name,
            "primary_color": profile.primary_color,
            "has_logo": bool(profile.logo_url),
            "has_favicon": bool(profile.favicon_url),
        }
        if args.apply:
            db.commit()
        else:
            db.rollback()
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
