"""Backfill `payments.payment_channel_id` and `.collection_account_id` from evidence.

Both columns are NULL fleet-wide because `payment_channels` and
`collection_accounts` were never seeded, so nothing could ever resolve. With them
seeded, historic payments can be attributed from evidence that already exists.

Two independent phases, reported separately:

**Phase 1 - Splynx (authoritative).** 98,417 of 99,182 payments carry a
`splynx_payment_id`, and the Splynx migration is verified complete (the archived
`payments` table holds exactly 98,417 rows). Joining
`splynx_billing_transactions.description` gives both the channel and, for bank and
cash payments, the specific account. Those description values are
`payments_types.name` from Splynx, so the mapping is authoritative rather than
inferred.

**Phase 2 - native by provider.** Post-Splynx payments have no Splynx row. Where
`provider_id` is set, the gateway is direct evidence of the channel. No account is
implied: gateway settlements land in a bank account that this data does not name.

Deliberately NOT done here: memo-string matching (``Bank transfer (proof ...)``,
``NIP/FBN/...``, ``TRF FROM ...``). Those are suggestive but not evidence, and a
wrong account is worse than a missing one for reconciliation. Payments left
unattributed are counted and reported so the gap stays visible rather than looking
like completed work.

Safety:

* dry run unless ``--apply``;
* never overwrites a non-NULL channel or account (``COALESCE`` + a WHERE that only
  matches rows actually needing a change), so re-running is idempotent and a
  correction made by hand is never clobbered;
* per-bucket counts printed before and after.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from app.db import SessionLocal

# splynx description -> (channel name, collection account name or None)
SPLYNX_MAP: dict[str, tuple[str, str | None]] = {
    "Paystack": ("Paystack", None),
    "Flutterwave": ("Flutterwave", None),
    "Remita": ("Remita", None),
    "Credit card": ("Card", None),
    "Other": ("Other", None),
    "Zenith 461 Bank": ("Bank Transfer", "Zenith 461 Bank"),
    "Zenith 523 Bank": ("Bank Transfer", "Zenith 523 Bank"),
    "UBA": ("Bank Transfer", "UBA"),
    "Dotmac USD": ("Bank Transfer", "Dotmac USD"),
    # Named no account on purpose: Splynx recorded a generic bank transfer without
    # identifying which account received it. Channel is known, account is not.
    "Bank transfer": ("Bank Transfer", None),
    "Cash CBD": ("Cash", "Cash CBD"),
    "Cash": ("Cash", "Cash"),
}

# provider_type -> channel name
PROVIDER_MAP: dict[str, str] = {
    "paystack": "Paystack",
    "flutterwave": "Flutterwave",
}

# Rows still needing a change. EXISTS (not a join) so the count cannot be inflated
# by a payment matching more than one transaction row, which would make the
# dry-run count disagree with what UPDATE ... FROM actually touches.
_SPLYNX_PRED = """
    p.splynx_payment_id IS NOT NULL
      AND EXISTS (
        SELECT 1 FROM splynx_billing_transactions t
        WHERE t.splynx_payment_id = p.splynx_payment_id
          AND t.description = :desc
      )
      AND (
            p.payment_channel_id IS NULL
         OR (CAST(:acct_id AS uuid) IS NOT NULL AND p.collection_account_id IS NULL)
      )
"""

_SPLYNX_UPDATE_FROM = """
    FROM splynx_billing_transactions t
    WHERE t.splynx_payment_id = p.splynx_payment_id
      AND t.description = :desc
      AND p.splynx_payment_id IS NOT NULL
      AND (
            p.payment_channel_id IS NULL
         OR (CAST(:acct_id AS uuid) IS NOT NULL AND p.collection_account_id IS NULL)
      )
"""

_PROVIDER_PRED = """
    p.splynx_payment_id IS NULL
      AND p.payment_channel_id IS NULL
      AND EXISTS (
        SELECT 1 FROM payment_providers pr
        WHERE pr.id = p.provider_id AND pr.provider_type = :ptype
      )
"""

_PROVIDER_UPDATE_FROM = """
    FROM payment_providers pr
    WHERE pr.id = p.provider_id
      AND pr.provider_type = :ptype
      AND p.splynx_payment_id IS NULL
      AND p.payment_channel_id IS NULL
"""


def _lookup(db, table: str, name: str) -> str | None:
    row = db.execute(
        text(f"select id from {table} where name = :name"), {"name": name}
    ).first()
    return str(row[0]) if row else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit the backfill")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        channels = {
            name: _lookup(db, "payment_channels", name)
            for name in {c for c, _ in SPLYNX_MAP.values()} | set(PROVIDER_MAP.values())
        }
        accounts = {
            name: _lookup(db, "collection_accounts", name)
            for _, name in SPLYNX_MAP.values()
            if name
        }
        missing = [n for n, v in {**channels, **accounts}.items() if v is None]
        if missing:
            print(f"ABORT - not seeded: {', '.join(sorted(missing))}")
            return 2

        before = db.execute(
            text(
                "select count(*) filter (where payment_channel_id is not null), "
                "count(*) filter (where collection_account_id is not null), count(*) "
                "from payments"
            )
        ).first()
        print(f"before: channel={before[0]}  account={before[1]}  total={before[2]}\n")

        total = 0
        print("Phase 1 - Splynx evidence")
        for desc, (channel_name, account_name) in SPLYNX_MAP.items():
            params = {
                "desc": desc,
                "ch_id": channels[channel_name],
                "acct_id": accounts[account_name] if account_name else None,
            }
            n = db.execute(
                text(f"SELECT count(*) FROM payments p WHERE {_SPLYNX_PRED}"), params
            ).scalar_one()
            if n and args.apply:
                db.execute(
                    text(
                        "UPDATE payments p SET "
                        "payment_channel_id = COALESCE(p.payment_channel_id, "
                        "CAST(:ch_id AS uuid)), "
                        "collection_account_id = COALESCE(p.collection_account_id, "
                        f"CAST(:acct_id AS uuid)) {_SPLYNX_UPDATE_FROM}"
                    ),
                    params,
                )
            total += n
            label = f"{channel_name}" + (f" / {account_name}" if account_name else "")
            print(f"  {desc:<18} -> {label:<32} {n:>7}")

        print("\nPhase 2 - native payments by provider")
        for ptype, channel_name in PROVIDER_MAP.items():
            params = {"ptype": ptype, "ch_id": channels[channel_name]}
            n = db.execute(
                text(f"SELECT count(*) FROM payments p WHERE {_PROVIDER_PRED}"), params
            ).scalar_one()
            if n and args.apply:
                db.execute(
                    text(
                        "UPDATE payments p SET payment_channel_id = "
                        f"CAST(:ch_id AS uuid) {_PROVIDER_UPDATE_FROM}"
                    ),
                    params,
                )
            total += n
            print(f"  provider {ptype:<12} -> {channel_name:<28} {n:>7}")

        if args.apply:
            db.commit()
            after = db.execute(
                text(
                    "select count(*) filter (where payment_channel_id is not null), "
                    "count(*) filter (where collection_account_id is not null), "
                    "count(*) from payments"
                )
            ).first()
            print(f"\nafter:  channel={after[0]}  account={after[1]}  total={after[2]}")
            unattributed = after[2] - after[0]
        else:
            db.rollback()
            unattributed = before[2] - before[0] - total

        verb = "Updated" if args.apply else "Would update"
        print(f"\n{verb}: {total} payment rows")
        print(
            f"Still without a channel: {unattributed} (memo-only evidence; "
            "left for a separate reviewed pass)"
        )
        if not args.apply:
            print("\nDRY RUN - nothing was written. Re-run with --apply to commit.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
