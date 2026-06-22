"""Backfill ``subscriptions.login`` from the subscriber's active PPPoE credential.

``AccessCredential.username`` is the source of truth for the PPPoE login;
``Subscription.login`` is a denormalized mirror. As of PR #360, activation binds
login to the minted credential, so new activations are consistent. This one-off
repairs the LEGACY population where login is empty but a single active credential
exists — the ``BACKFILLABLE`` bucket from
``audit_subscription_login_credential_drift``.

Why it matters: the periodic ``radius_population`` reconciler keys on
``Subscription.login`` and silently drops empty-login subs, and customer-detail /
search / enforcement readers degrade without it.

Scope / safety
--------------
* ONLY touches active subscriptions with an empty login AND exactly one active
  credential (sets login = that credential's username). It re-derives nothing.
* Deliberately SKIPS:
    - STALE (login already set — never overwrites)
    - AMBIGUOUS (>1 active credential — no safe single choice)
    - NO_CREDENTIAL (no credential to mirror — a provisioning gap, not a login fix)
* ``login`` has no unique constraint; when one subscriber has several active
  subscriptions sharing a single credential they all get the same login, which
  mirrors reality (one credential) and matches how CONSISTENT rows already look.
* Idempotent: a second run finds nothing (those rows are now CONSISTENT).
* Classification is imported from the audit module so backfill == what the audit
  reports — one source of truth.

Dry-run by default; nothing is written without --apply.

Examples
--------
  # Preview (read-only):
  python -m scripts.one_off.backfill_subscription_login_from_credential

  # Apply:
  python -m scripts.one_off.backfill_subscription_login_from_credential --apply
"""

from __future__ import annotations

import argparse

from app.db import SessionLocal
from app.models.catalog import Subscription
from scripts.one_off.audit_subscription_login_credential_drift import collect


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes. Without this flag the run is read-only.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap the number of rows changed (0 = no cap).",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        backfillable = collect(session)["BACKFILLABLE"]
        if args.limit > 0:
            backfillable = backfillable[: args.limit]

        if not backfillable:
            print("Nothing to backfill — no active subscription has an empty login "
                  "with exactly one active credential.")
            return 0

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] {len(backfillable)} subscription(s) to backfill:\n")

        changed = 0
        skipped = 0
        for row in backfillable:
            sub_id = row["subscription_id"]
            target = row["would_set_login_to"]
            sub = session.get(Subscription, sub_id)
            if sub is None:
                print(f"  SKIP {sub_id[:8]} — subscription vanished")
                skipped += 1
                continue
            # Defensive re-check at write time: never overwrite a login that
            # was set between audit collection and now.
            if str(sub.login or "").strip():
                print(f"  SKIP {sub_id[:8]} — login now set to {sub.login!r}")
                skipped += 1
                continue
            print(f"  {sub_id[:8]}  login '' -> {target}")
            if args.apply:
                sub.login = target
            changed += 1

        if args.apply:
            session.commit()
            print(f"\nApplied: {changed} updated, {skipped} skipped.")
        else:
            print(f"\nDry-run: {changed} would be updated, {skipped} skipped. "
                  f"Re-run with --apply to write.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
