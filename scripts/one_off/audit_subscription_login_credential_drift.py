"""Audit drift between ``subscriptions.login`` and active PPPoE credentials.

Context
-------
``AccessCredential.username`` is the source of truth for a subscriber's PPPoE
login; ``Subscription.login`` is a *denormalized mirror* of it. As of PR #360,
activation binds ``login`` to the credential actually minted, so all NEW
activations are consistent. This audit quantifies the LEGACY population so we
can decide whether a one-off backfill is worth building.

Why ``login`` matters even though it's denormalized
---------------------------------------------------
* Per-activation RADIUS write (reconcile_subscription_connectivity) reads from
  ``AccessCredential`` — so connectivity at activation does NOT depend on login.
* BUT the periodic ``radius_population`` reconciler keys on ``Subscription.login``
  and silently drops empty-login subs (``active_usernames = {sub.login ...}``),
  and customer-detail/search/enforcement readers degrade with an empty login.

Categories reported (active subscriptions only)
-----------------------------------------------
  CONSISTENT         login matches an active AccessCredential.username (healthy)
  BACKFILLABLE       login empty AND exactly one active credential -> safe to
                     set login = that credential's username
  AMBIGUOUS          login empty AND >1 active credential -> needs a rule
  NO_CREDENTIAL      login empty AND no active credential -> NOT a login problem;
                     a provisioning gap (no credential to mirror)
  STALE             login set but matches NO active credential.username -> points
                     at a username with no active credential (drift)

Read-only. Nothing is written. Decide on a backfill from the counts:
BACKFILLABLE is the population a one-off would fix.

Examples
--------
  python -m scripts.one_off.audit_subscription_login_credential_drift
  python -m scripts.one_off.audit_subscription_login_credential_drift --sample 30
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from app.db import SessionLocal
from app.models.catalog import AccessCredential, Subscription, SubscriptionStatus

CATEGORIES = ("CONSISTENT", "BACKFILLABLE", "AMBIGUOUS", "NO_CREDENTIAL", "STALE")


def _norm(value: str | None) -> str:
    return str(value or "").strip()


def collect(session) -> dict[str, list[dict]]:
    """Classify every active subscription by login/credential consistency.

    Pure read; returns the bucketed rows so callers (and tests) can inspect
    counts without driving the CLI printer.
    """
    # Active credential usernames grouped by subscriber.
    creds_by_subscriber: dict[object, list[str]] = defaultdict(list)
    for subscriber_id, username in session.query(
        AccessCredential.subscriber_id, AccessCredential.username
    ).filter(AccessCredential.is_active.is_(True)):
        uname = _norm(username)
        if uname:
            creds_by_subscriber[subscriber_id].append(uname)

    active_subs = session.query(
        Subscription.id,
        Subscription.subscriber_id,
        Subscription.login,
    ).filter(Subscription.status == SubscriptionStatus.active)

    buckets: dict[str, list[dict]] = {name: [] for name in CATEGORIES}
    for sub_id, subscriber_id, login in active_subs:
        login_v = _norm(login)
        usernames = creds_by_subscriber.get(subscriber_id, [])
        row = {
            "subscription_id": str(sub_id),
            "subscriber_id": str(subscriber_id),
            "login": login_v,
            "active_credential_usernames": usernames,
        }
        if login_v:
            if login_v in usernames:
                buckets["CONSISTENT"].append(row)
            else:
                buckets["STALE"].append(row)
        else:
            if len(usernames) == 1:
                row["would_set_login_to"] = usernames[0]
                buckets["BACKFILLABLE"].append(row)
            elif len(usernames) > 1:
                buckets["AMBIGUOUS"].append(row)
            else:
                buckets["NO_CREDENTIAL"].append(row)
    return buckets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        type=int,
        default=20,
        help="Rows to print per actionable category (default 20).",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        buckets = collect(session)

        total = sum(len(v) for v in buckets.values())
        print(f"\nActive subscriptions audited: {total}\n")
        print(f"{'category':<14} {'count':>7}  description")
        print("-" * 60)
        descriptions = {
            "CONSISTENT": "login mirrors an active credential (healthy)",
            "BACKFILLABLE": "login empty, exactly 1 active cred -> fixable",
            "AMBIGUOUS": "login empty, >1 active cred -> needs a rule",
            "NO_CREDENTIAL": "login empty, no active cred -> provisioning gap",
            "STALE": "login set but matches no active cred -> drift",
        }
        for name in (
            "CONSISTENT",
            "BACKFILLABLE",
            "AMBIGUOUS",
            "STALE",
            "NO_CREDENTIAL",
        ):
            print(f"{name:<14} {len(buckets[name]):>7}  {descriptions[name]}")

        for name in ("BACKFILLABLE", "STALE", "AMBIGUOUS", "NO_CREDENTIAL"):
            rows = buckets[name]
            if not rows:
                continue
            print(
                f"\n--- {name} (showing {min(args.sample, len(rows))} of {len(rows)}) ---"
            )
            for row in rows[: args.sample]:
                target = (
                    f"  -> {row['would_set_login_to']}"
                    if "would_set_login_to" in row
                    else ""
                )
                print(
                    f"  sub={row['subscription_id']} "
                    f"login={row['login']!r} "
                    f"creds={row['active_credential_usernames']}{target}"
                )

        backfillable = len(buckets["BACKFILLABLE"])
        print(
            f"\nVerdict: {backfillable} subscription(s) are safely backfillable "
            f"(login empty + single active credential)."
        )
        if backfillable == 0:
            print(
                "No backfill needed — new activations are already consistent "
                "(PR #360). STALE/AMBIGUOUS/NO_CREDENTIAL are separate concerns."
            )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
