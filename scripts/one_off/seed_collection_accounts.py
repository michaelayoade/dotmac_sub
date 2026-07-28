"""Seed collection accounts and map them to payment channels.

This repair/seed complements migration 373: it ensures the full attribution
directory and channel mappings exist after the two customer-presented Zenith
destinations have moved into `collection_accounts`. It is safe whether the
attribution rows or migration 373 land first.

The account set below comes from the authoritative Splynx `payments_types` table
(archived dumps on seabone), cross-checked against the descriptions actually
present on 98,413 migrated payments. Names are kept **verbatim from Splynx** so
the backfill mapping is self-evident and staff recognise them; they can be
renamed later through the existing admin CRUD without breaking anything, since
mapping is by id once seeded.

Deliberate omissions for accounts not used in customer presentment:

* No invented `account_number` / `account_name`. Migration 373 supplies the two
  Zenith destinations from checked-in legacy facts. UBA and the USD account
  remain attribution identities until authoritative payment details are entered
  through the collection-account owner.
* `account_last4` is set only for the two Zenith accounts, where the digits are
  known from the live `direct_bank_transfer_accounts` setting. UBA and the USD
  account have no digits on record anywhere — inventing them would be fabricating
  evidence.

Resolver note: `_resolve_collection_account` filters `payment_channel_accounts`
by currency first (ordered by `is_default desc, priority desc`), then falls back
to rows with a NULL currency. Currencies are therefore set explicitly so a USD
payment cannot resolve to an NGN account.

Idempotent: accounts match on the unique `name`, mappings on
(channel, account, currency). Dry-run unless ``--apply``.
"""

from __future__ import annotations

import argparse
import sys

from app.db import SessionLocal
from app.models.billing import (
    CollectionAccount,
    CollectionAccountType,
    PaymentChannel,
    PaymentChannelAccount,
)

# (name, account_type, bank_name, account_last4, currency, notes)
ACCOUNTS: tuple[
    tuple[str, CollectionAccountType, str | None, str | None, str, str], ...
] = (
    (
        "Zenith 461 Bank",
        CollectionAccountType.bank,
        "ZENITH BANK",
        "6461",
        "NGN",
        "Primary NGN collection account. Splynx label 'Zenith 461 Bank'; "
        "QuickBooks GL 397.",
    ),
    (
        "Zenith 523 Bank",
        CollectionAccountType.bank,
        "ZENITH BANK",
        "9523",
        "NGN",
        "Secondary NGN collection account. Splynx label 'Zenith 523 Bank'; "
        "QuickBooks GL 395.",
    ),
    (
        "UBA",
        CollectionAccountType.bank,
        "UBA",
        None,
        "NGN",
        "Low-volume NGN account (16 historic payments). Account digits are not "
        "recorded anywhere in sub or the Splynx archive.",
    ),
    (
        "Dotmac USD",
        CollectionAccountType.bank,
        None,
        None,
        "USD",
        "USD collection account (16 historic payments). Splynx GL 445 "
        "('Zenith USD'); bank and digits not recorded.",
    ),
    (
        "Cash CBD",
        CollectionAccountType.cash,
        None,
        None,
        "NGN",
        "Cash collected at the CBD office. Splynx GL 342.",
    ),
    (
        "Cash",
        CollectionAccountType.cash,
        None,
        None,
        "NGN",
        "General cash collection, not tied to a named location.",
    ),
)

# (channel_name, account_name, currency, priority, is_default)
MAPPINGS: tuple[tuple[str, str, str, int, bool], ...] = (
    ("Bank Transfer", "Zenith 461 Bank", "NGN", 100, True),
    ("Bank Transfer", "Zenith 523 Bank", "NGN", 90, False),
    ("Bank Transfer", "UBA", "NGN", 50, False),
    ("Bank Transfer", "Dotmac USD", "USD", 100, True),
    ("Cash", "Cash CBD", "NGN", 100, True),
    ("Cash", "Cash", "NGN", 50, False),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit the seed (default is a dry run that changes nothing)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    created_accounts: list[str] = []
    created_mappings: list[str] = []
    skipped: list[str] = []
    try:
        for name, acct_type, bank_name, last4, currency, notes in ACCOUNTS:
            existing = (
                db.query(CollectionAccount)
                .filter(CollectionAccount.name == name)
                .first()
            )
            if existing is None and last4:
                last4_matches = (
                    db.query(CollectionAccount)
                    .filter(
                        CollectionAccount.account_type == acct_type,
                        CollectionAccount.bank_name == bank_name,
                        CollectionAccount.account_last4 == last4,
                        CollectionAccount.currency == currency,
                    )
                    .all()
                )
                if len(last4_matches) > 1:
                    raise RuntimeError(
                        f"ambiguous existing collection-account identity for ****{last4}"
                    )
                if last4_matches:
                    existing = last4_matches[0]
                    # Migration 373 may have created the payment destination
                    # before this attribution seed runs. Adopt that identity and
                    # give it the authoritative Splynx label instead of creating
                    # a duplicate row for the same real account.
                    existing.name = name
                    existing.bank_name = existing.bank_name or bank_name
                    existing.notes = existing.notes or notes
            if existing is not None:
                skipped.append(f"account {name} (already present)")
                continue
            db.add(
                CollectionAccount(
                    name=name,
                    account_type=acct_type,
                    bank_name=bank_name,
                    account_last4=last4,
                    currency=currency,
                    is_active=True,
                    notes=notes,
                )
            )
            created_accounts.append(
                f"{name} [{acct_type.value}/{currency}]"
                + (f" ****{last4}" if last4 else "")
            )
        db.flush()

        channels = {c.name: c for c in db.query(PaymentChannel).all()}
        accounts = {a.name: a for a in db.query(CollectionAccount).all()}

        for channel_name, account_name, currency, priority, is_default in MAPPINGS:
            channel = channels.get(channel_name)
            account = accounts.get(account_name)
            if channel is None or account is None:
                skipped.append(
                    f"mapping {channel_name} -> {account_name} "
                    f"(missing {'channel' if channel is None else 'account'})"
                )
                continue
            exists = (
                db.query(PaymentChannelAccount)
                .filter(
                    PaymentChannelAccount.channel_id == channel.id,
                    PaymentChannelAccount.collection_account_id == account.id,
                    PaymentChannelAccount.currency == currency,
                )
                .first()
            )
            if exists is not None:
                skipped.append(
                    f"mapping {channel_name} -> {account_name} ({currency}) "
                    "(already present)"
                )
                continue
            db.add(
                PaymentChannelAccount(
                    channel_id=channel.id,
                    collection_account_id=account.id,
                    currency=currency,
                    priority=priority,
                    is_default=is_default,
                    is_active=True,
                )
            )
            created_mappings.append(
                f"{channel_name} -> {account_name} ({currency}, priority "
                f"{priority}{', default' if is_default else ''})"
            )

        if args.apply:
            db.commit()
        else:
            db.rollback()
    finally:
        db.close()

    verb = "Created" if args.apply else "Would create"
    print(f"{verb} accounts: {len(created_accounts)}")
    for line in created_accounts:
        print(f"  + {line}")
    print(f"{verb} channel mappings: {len(created_mappings)}")
    for line in created_mappings:
        print(f"  + {line}")
    if skipped:
        print(f"Skipped: {len(skipped)}")
        for line in skipped:
            print(f"  - {line}")
    if not args.apply:
        print("\nDRY RUN - nothing was written. Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
