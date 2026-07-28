"""Move customer-facing bank details out of settings into `collection_accounts`.

Presentment now reads `collection_accounts` (see
`app/services/billing/collection_account_directory.py`), so the details held in
`billing.direct_bank_transfer_accounts` must be carried across or the portal,
reseller portal, `/api/me` and invoices will show nothing.

Matching is by **account_number last 4**, against the accounts already seeded from
Splynx evidence. That is deliberate: the seeded rows already carry the Splynx
identity and history (`Zenith 461 Bank` holds 24,568 attributed payments), so this
enriches them rather than creating a second row for the same real account. A
settings entry whose last4 matches nothing is reported, never auto-created —
an unrecognised account needs a human to say what it is.

Run AFTER migration 373 and AFTER `seed_collection_accounts.py`. Idempotent: an
account that already has an `account_number` is left alone. Dry run unless
``--apply``.
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models.billing import CollectionAccount
from app.models.domain_settings import DomainSetting, SettingDomain

SETTING_KEY = "direct_bank_transfer_accounts"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit the migration")
    args = parser.parse_args()

    db = SessionLocal()
    updated: list[str] = []
    unmatched: list[str] = []
    skipped: list[str] = []
    try:
        row = db.scalars(
            select(DomainSetting)
            .where(DomainSetting.domain == SettingDomain.billing)
            .where(DomainSetting.key == SETTING_KEY)
        ).first()
        if row is None or not (row.value_text or "").strip():
            print(f"Setting {SETTING_KEY!r} is absent or empty - nothing to migrate.")
            return 0

        try:
            entries = json.loads(row.value_text)
        except json.JSONDecodeError:
            print(f"ABORT - {SETTING_KEY!r} is not valid JSON.")
            return 2
        if not isinstance(entries, list):
            print(f"ABORT - {SETTING_KEY!r} is not a list.")
            return 2

        accounts = db.scalars(select(CollectionAccount)).all()
        by_last4: dict[str, CollectionAccount] = {}
        for account in accounts:
            last4 = (account.account_last4 or "").strip()
            if not last4:
                continue
            if last4 in by_last4:
                print(
                    "ABORT - multiple collection accounts share last4 "
                    f"****{last4}; resolve identity before repair."
                )
                return 2
            by_last4[last4] = account

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            number = str(entry.get("account_number") or "").strip()
            name = str(entry.get("account_name") or "").strip()
            bank = str(entry.get("bank_name") or "").strip()
            sort_code = str(entry.get("sort_code") or "").strip()
            if not number:
                continue
            target = by_last4.get(number[-4:])
            if target is None:
                unmatched.append(f"****{number[-4:]} ({bank} / {name})")
                continue
            if (target.account_number or "").strip():
                skipped.append(f"{target.name} (already has an account number)")
                continue
            target.account_number = number
            target.account_name = name or target.account_name
            target.bank_name = bank or target.bank_name
            target.sort_code = sort_code or target.sort_code
            updated.append(f"{target.name} <- ****{number[-4:]} ({bank} / {name})")

        if args.apply:
            db.commit()
        else:
            db.rollback()
    finally:
        db.close()

    verb = "Enriched" if args.apply else "Would enrich"
    print(f"{verb}: {len(updated)}")
    for line in updated:
        print(f"  + {line}")
    if skipped:
        print(f"Skipped: {len(skipped)}")
        for line in skipped:
            print(f"  - {line}")
    if unmatched:
        print(f"UNMATCHED (no collection account with that last4): {len(unmatched)}")
        for line in unmatched:
            print(f"  ! {line}")
        print("  Create these via the collection-accounts admin, then re-run.")
    if not args.apply:
        print("\nDRY RUN - nothing was written. Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
