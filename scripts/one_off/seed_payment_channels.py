"""Seed the canonical payment channels.

``payment_channels`` shipped with a correct schema, a working resolver
(``_resolve_payment_channel``) and a reporting join, but was never populated.
Because the table is empty every resolution attempt returns ``None``, so
``payments.payment_channel_id`` is NULL fleet-wide and channel-level
reconciliation against Paystack settlements or bank statements is impossible.

This seeds one row per real-world channel evidenced in production payment data.

Safety notes that shaped the row set:

* ``_resolve_payment_channel`` raises HTTP 400 ("Multiple payment channels match
  provider; set a default") when a provider has more than one active channel and
  none is marked default. Every provider-backed channel here is therefore created
  with ``is_default=True``, and there is exactly one channel per provider.
* ``is_default`` is scoped per ``provider_id`` (the create path demotes prior
  defaults for the same provider), so defaults on different providers do not
  collide.
* ``default_collection_account_id`` is deliberately left NULL: ``collection_accounts``
  is empty in production. Settlement/fee reconciliation needs those seeded
  separately, and inventing bank accounts here would be fabricating evidence.
* Channel type for the gateways is ``other`` rather than ``card``: Paystack and
  Flutterwave both accept card, bank transfer and USSD, and ``provider_id``
  already carries their precise identity.

Idempotent: rows are matched on the unique ``name`` and skipped if present.
Dry-run unless ``--apply`` is passed.
"""

from __future__ import annotations

import argparse
import sys

from app.db import SessionLocal
from app.models.billing import PaymentChannel, PaymentChannelType, PaymentProvider

# (name, channel_type, provider_type or None, notes)
CHANNELS: tuple[tuple[str, PaymentChannelType, str | None, str], ...] = (
    (
        "Paystack",
        PaymentChannelType.other,
        "paystack",
        "Online gateway (card / bank transfer / USSD). Settlement arrives net of "
        "provider fees; reconcile against Paystack settlement reports.",
    ),
    (
        "Flutterwave",
        PaymentChannelType.other,
        "flutterwave",
        "Online gateway. Provider is active; seeded so future payments resolve a "
        "channel instead of NULL.",
    ),
    (
        "Bank Transfer",
        PaymentChannelType.bank_transfer,
        None,
        "Direct/NIP transfer into a Dotmac collection account, including "
        "proof-of-payment submissions. No gateway integration.",
    ),
    (
        "Cash",
        PaymentChannelType.cash,
        None,
        "Cash collected at office or by field staff.",
    ),
    # Added 2026-07-20 from the authoritative Splynx `payments_types` list, which
    # showed the original four-channel seed was incomplete. No provider records
    # exist for these, so they are set explicitly or resolved by payment method.
    (
        "Remita",
        PaymentChannelType.other,
        None,
        "Remita gateway. Historic Splynx channel; no provider integration in sub.",
    ),
    (
        "Card",
        PaymentChannelType.card,
        None,
        "Direct card capture (Splynx 'Credit card'). Distinct from card payments "
        "taken through a gateway, which belong to that gateway's channel.",
    ),
    (
        "Other",
        PaymentChannelType.other,
        None,
        "Explicit catch-all, so an unclassifiable payment is recorded as 'Other' "
        "rather than left NULL and indistinguishable from an unattributed one.",
    ),
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
    created: list[str] = []
    skipped: list[str] = []
    try:
        providers = {
            provider.provider_type.value
            if hasattr(provider.provider_type, "value")
            else str(provider.provider_type): provider
            for provider in db.query(PaymentProvider).all()
        }

        for name, channel_type, provider_type, notes in CHANNELS:
            existing = (
                db.query(PaymentChannel).filter(PaymentChannel.name == name).first()
            )
            if existing is not None:
                skipped.append(f"{name} (already present)")
                continue

            provider = None
            if provider_type is not None:
                provider = providers.get(provider_type)
                if provider is None:
                    skipped.append(f"{name} (no active provider {provider_type!r})")
                    continue

            db.add(
                PaymentChannel(
                    name=name,
                    channel_type=channel_type,
                    provider_id=provider.id if provider else None,
                    default_collection_account_id=None,
                    is_active=True,
                    # Per-provider default: guarantees the resolver never hits the
                    # "multiple channels match provider" 400 for this provider.
                    is_default=provider is not None,
                    notes=notes,
                )
            )
            created.append(
                f"{name} [{channel_type.value}]"
                + (
                    f" -> provider {provider_type}"
                    if provider_type
                    else " (no provider)"
                )
            )

        if args.apply:
            db.commit()
        else:
            db.rollback()
    finally:
        db.close()

    verb = "Created" if args.apply else "Would create"
    print(f"{verb}: {len(created)}")
    for line in created:
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
