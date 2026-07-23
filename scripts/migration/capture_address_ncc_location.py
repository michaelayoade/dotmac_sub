#!/usr/bin/env python3
"""Backfill NCC location (state=region + LGA) onto coord-bearing Addresses.

For each service ``Address`` that has coordinates but is missing ``region`` or
``lga``, reverse-geocode the pin via ``geocode_reconciler.reverse`` (the
customer.location_verification owner, against the self-hosted Nominatim) and
validate the result through ``ncc_location``. Sets ``Address.region`` (canonical
NCC state) and ``Address.lga`` (validated). Idempotent — re-runs only touch rows
still missing a value.

Run inside the app container (so ``nominatim:8080`` resolves and
``integration.nominatim_base_url`` is set):

    docker exec -w /app dotmac_sub_app python /app/capture_address_ncc_location.py --dry-run
    docker exec -w /app dotmac_sub_app python /app/capture_address_ncc_location.py
"""

from __future__ import annotations

import argparse
import json

from app.db import SessionLocal
from app.models.subscriber import Address
from app.services import geocode_reconciler as reconciler
from app.services import ncc_location


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch-size", type=int, default=100)
    args = ap.parse_args()

    db = SessionLocal()
    query = (
        db.query(Address)
        .filter(Address.latitude.isnot(None), Address.longitude.isnot(None))
        .filter((Address.region.is_(None)) | (Address.lga.is_(None)))
        .order_by(Address.created_at.asc())
    )
    if args.limit:
        query = query.limit(args.limit)
    rows = query.all()

    counts = {
        "candidates": len(rows),
        "reverse_ok": 0,
        "no_result": 0,
        "region_set": 0,
        "lga_set": 0,
    }
    try:
        for i, addr in enumerate(rows, 1):
            result = reconciler.reverse(db, float(addr.latitude), float(addr.longitude))
            if not result or not result.state:
                counts["no_result"] += 1
                continue
            counts["reverse_ok"] += 1
            state = ncc_location.canonical_state(result.state)
            if not state:
                continue
            lga = ncc_location.canonical_lga(state, result.lga)
            if addr.region != state:
                counts["region_set"] += 1
                if not args.dry_run:
                    addr.region = state
            if lga and addr.lga != lga:
                counts["lga_set"] += 1
                if not args.dry_run:
                    addr.lga = lga
            if not args.dry_run and i % args.batch_size == 0:
                db.commit()
        if not args.dry_run:
            db.commit()
    finally:
        db.close()

    print(json.dumps({"dry_run": args.dry_run, **counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
