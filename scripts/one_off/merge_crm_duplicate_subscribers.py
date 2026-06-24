"""CLI for the CRM duplicate-subscriber merge (see crm_duplicate_merge).

DESTRUCTIVE on the CRM when run without --dry-run: re-points tickets/work
orders from each erpnext alias record to the imported primary, then
soft-deletes the alias. Start with --dry-run, then a small --limit canary.

Usage:
    python -m scripts.one_off.merge_crm_duplicate_subscribers --dry-run
    python -m scripts.one_off.merge_crm_duplicate_subscribers --limit 10
    python -m scripts.one_off.merge_crm_duplicate_subscribers --live
"""

from __future__ import annotations

import argparse
import logging

from app.db import SessionLocal
from app.services.crm_duplicate_merge import merge_duplicates


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="count only")
    mode.add_argument("--live", action="store_true", help="perform the merge")
    parser.add_argument(
        "--limit", type=int, default=None, help="max subscribers to process"
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        stats = merge_duplicates(db, dry_run=not args.live, limit=args.limit)
    finally:
        db.close()
    prefix = "[dry-run] " if not args.live else ""
    print(f"{prefix}{stats}")


if __name__ == "__main__":
    main()
