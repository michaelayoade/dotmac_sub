"""Restore service to paid-up-but-walled accounts (account-level, no ledger writes).

The safe replacement for the per-invoice credit-settle backfill. Keys on the
authoritative account-level net (``get_available_balance`` — imported deposit for
migrated accounts, ledger for native), NOT per-invoice balance_due. A walled
(suspended/blocked) account whose available balance is >= 0 is paid up and gets
its service restored: reason-scoped status restore + re-derive + RADIUS refresh
+ CoA. No money/ledger changes. See app/services/billing/unwall_paid_accounts.py.

Dry-run by default; nothing written without --apply.

  python -m scripts.one_off.unwall_paid_accounts                       # dry-run
  python -m scripts.one_off.unwall_paid_accounts --apply --limit 1     # stage
  python -m scripts.one_off.unwall_paid_accounts --apply \
      --logins 100015097,100017641,100023828                           # reported set
"""

from __future__ import annotations

import argparse

from app.db import SessionLocal
from app.models.catalog import Subscription
from app.services.billing.unwall_paid_accounts import unwall_cohort


def _resolve_login_subscription_ids(db, logins: list[str]) -> list[str]:
    if not logins:
        return []
    rows = db.query(Subscription.id).filter(Subscription.login.in_(logins)).all()
    return [str(r[0]) for r in rows]


def _resolve_login_account_ids(db, logins: list[str]) -> list[str]:
    if not logins:
        return []
    rows = (
        db.query(Subscription.subscriber_id)
        .filter(Subscription.login.in_(logins))
        .distinct()
        .all()
    )
    return [str(r[0]) for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Write changes (else read-only)."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap accounts processed."
    )
    parser.add_argument("--no-radius", action="store_true", help="Skip RADIUS rebuild.")
    parser.add_argument("--no-coa", action="store_true", help="Skip CoA session kicks.")
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Send 'service resumed' notifications (off by default for bulk catch-up).",
    )
    parser.add_argument(
        "--restore-logins",
        default="",
        help=(
            "TARGETED mode: restore ONLY these comma-separated logins' accounts "
            "(paid-up gated), instead of the full walled cohort. Use this to "
            "un-wall a specific reported set first."
        ),
    )
    parser.add_argument(
        "--logins",
        default="",
        help="Comma-separated logins to additionally force RADIUS+CoA onto.",
    )
    args = parser.parse_args()
    dry_run = not args.apply
    restore_logins = [s.strip() for s in args.restore_logins.split(",") if s.strip()]
    logins = [s.strip() for s in args.logins.split(",") if s.strip()]

    db = SessionLocal()
    try:
        account_ids = None
        if restore_logins:
            account_ids = _resolve_login_account_ids(db, restore_logins)
            if not account_ids:
                print(f"WARNING: no account matched --restore-logins: {restore_logins}")
        extra = _resolve_login_subscription_ids(db, logins)
        if logins and not extra:
            print(f"WARNING: no subscription matched --logins: {logins}")
        summary = unwall_cohort(
            db,
            account_ids=account_ids,
            limit=args.limit,
            dry_run=dry_run,
            refresh_radius=not args.no_radius,
            send_coa=not args.no_coa,
            notify=args.notify,
            extra_subscription_ids=extra,
        )
    finally:
        db.close()

    mode = "DRY-RUN (no changes written)" if dry_run else "APPLY"
    print(f"\n=== Un-wall paid-up accounts — {mode} ===")
    print(f"walled + paid-up candidates : {summary.candidates}")
    if not dry_run:
        print(f"accounts restored           : {summary.restored}")
        print(f"errors                      : {summary.errors}")
        print(f"radius refreshed            : {summary.radius_refreshed}")
        print(f"sessions kicked (CoA)       : {summary.sessions_kicked}")
    rows = [r for r in summary.results if r.error or dry_run or r.restored]
    if rows:
        print("\n--- per-account (status / available_balance) ---")
        for r in rows[:50]:
            note = f" ERROR: {r.error}" if r.error else ""
            after = f" -> {r.new_status}" if r.new_status else ""
            print(
                f"  {r.account_id[:8]}: {r.prior_status}{after} "
                f"avail={r.available_balance}{note}"
            )
    if dry_run:
        print("\nRe-run with --apply to restore service to these accounts.")


if __name__ == "__main__":
    main()
