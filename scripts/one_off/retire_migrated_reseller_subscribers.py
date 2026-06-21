"""Layer 3 Phase 4 — retire the orphaned reseller Subscriber rows after cutover.

After the Phase 2 backfill + cutover, each reseller login authenticates as a
ResellerUser principal; the old ``Subscriber`` (user_type=reseller) row it used to
be is now orphaned (its credential moved to reseller_user_id). This soft-deletes
those orphaned rows (status=canceled, is_active=False) so they stop appearing in
customer lists — which lets the ``user_type != reseller`` query filters be removed
(a follow-up). It only touches a reseller subscriber that has NO active local
credential left (i.e. one that was actually migrated), never a still-live login.

⚠️ This removes the cutover's easy rollback: once a reseller subscriber is
retired, ``backfill_reseller_user_principals --rollback`` would repoint the
credential to a canceled subscriber. So run this only AFTER the cutover has baked.
``--rollback`` here un-retires (restores the prior status) to keep it reversible.

Dry-run by default; nothing is written without --apply / --rollback.

Examples
--------
  python -m scripts.one_off.retire_migrated_reseller_subscribers            # dry-run
  python -m scripts.one_off.retire_migrated_reseller_subscribers --apply
  python -m scripts.one_off.retire_migrated_reseller_subscribers --rollback
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from app.db import SessionLocal
from app.models.auth import AuthProvider, UserCredential
from app.models.subscriber import Subscriber, SubscriberStatus, UserType

_PRIOR_STATUS_KEY = "_layer3_retire_prior_status"


def _has_active_local_credential(db, subscriber_id) -> bool:
    return (
        db.scalar(
            select(UserCredential.id)
            .where(UserCredential.subscriber_id == subscriber_id)
            .where(UserCredential.provider == AuthProvider.local)
            .where(UserCredential.is_active.is_(True))
        )
        is not None
    )


def _candidates(db) -> list[Subscriber]:
    subs = db.scalars(
        select(Subscriber).where(Subscriber.user_type == UserType.reseller)
    ).all()
    # Only migrated logins: no active local credential still on the subscriber.
    return [s for s in subs if not _has_active_local_credential(db, s.id)]


def _apply(db) -> None:
    rows = _candidates(db)
    skipped = [
        s
        for s in db.scalars(
            select(Subscriber).where(Subscriber.user_type == UserType.reseller)
        ).all()
        if _has_active_local_credential(db, s.id)
    ]
    print(f"Retiring {len(rows)} migrated reseller subscriber(s); "
          f"{len(skipped)} skipped (still have an active login). (APPLY)")
    n = 0
    for s in rows:
        if s.status == SubscriberStatus.canceled:
            continue
        meta = dict(s.metadata_ or {})
        meta.setdefault(_PRIOR_STATUS_KEY, getattr(s.status, "value", str(s.status)))
        s.metadata_ = meta
        s.status = SubscriberStatus.canceled
        s.is_active = False
        print(f"  retired {s.email} ({s.id})")
        n += 1
    db.commit()
    print(f"Committed: {n} retired.")


def _rollback(db) -> None:
    rows = db.scalars(
        select(Subscriber)
        .where(Subscriber.user_type == UserType.reseller)
        .where(Subscriber.status == SubscriberStatus.canceled)
    ).all()
    print(f"Un-retiring {len(rows)} reseller subscriber(s) (ROLLBACK)")
    n = 0
    for s in rows:
        meta = dict(s.metadata_ or {})
        prior = meta.pop(_PRIOR_STATUS_KEY, "active")
        try:
            s.status = SubscriberStatus(prior)
        except ValueError:
            s.status = SubscriberStatus.active
        s.is_active = True
        s.metadata_ = meta
        n += 1
    db.commit()
    print(f"Restored {n}.")


def _dry_run(db) -> None:
    rows = _candidates(db)
    print(f"Would retire {len(rows)} migrated reseller subscriber(s) (DRY-RUN):")
    for s in rows:
        print(f"  {s.email} ({s.id}) status={getattr(s.status, 'value', s.status)}")
    print("Re-run with --apply AFTER the cutover has baked (removes easy rollback).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--rollback", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.rollback:
            _rollback(db)
        elif args.apply:
            _apply(db)
        else:
            _dry_run(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
