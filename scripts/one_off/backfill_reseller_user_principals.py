"""Layer 3 Phase 2 — migrate reseller logins to first-class ResellerUser principals.

Today a reseller portal login is a ``Subscriber`` with ``user_type=reseller``,
linked to a ``Reseller`` via ``reseller_users`` and authenticated by a
``UserCredential`` keyed on ``subscriber_id``. Layer 3 makes the login its own
identity: this repoints each reseller's credential (and any MFA) from the
subscriber to a ``ResellerUser`` row that carries the login's identity.

What it does, per active reseller subscriber (``user_type=reseller``):
  1. get-or-create its ``ResellerUser`` row; populate ``email`` / ``full_name``
     from the subscriber; keep ``subscriber_id`` (person_id) for historical
     linkage + rollback.
  2. repoint its local ``UserCredential``: subscriber_id -> reseller_user_id.
  3. repoint its ``mfa_methods`` rows the same way.
The reseller ``Subscriber`` row itself is left in place (retired in Phase 4), so
already-issued portal sessions keep resolving via the subscriber path.

CUTOVER COORDINATION: a repointed credential only authenticates when
``RESELLER_USER_PRINCIPAL_ENABLED`` is ON (auth_flow gates the reseller_user
principal on the flag). Run ``--apply`` and flip the flag in the SAME window, or
reseller *new* logins fail until the flag is on (existing sessions are
unaffected). ``--rollback`` reverses the repoint (use with the flag OFF).

Dry-run by default; nothing is written without --apply / --rollback.

Examples
--------
  python -m scripts.one_off.backfill_reseller_user_principals            # dry-run
  python -m scripts.one_off.backfill_reseller_user_principals --apply
  python -m scripts.one_off.backfill_reseller_user_principals --rollback
"""

from __future__ import annotations

import argparse

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models.auth import MFAMethod, UserCredential
from app.models.subscriber import ResellerUser, Subscriber, UserType


def _full_name(sub: Subscriber) -> str | None:
    name = (sub.display_name or "").strip() or (
        f"{sub.first_name or ''} {sub.last_name or ''}".strip()
    )
    return name or None


def _get_or_create_reseller_user(db, sub: Subscriber, *, apply: bool) -> ResellerUser:
    ru = db.scalars(
        select(ResellerUser)
        .where(ResellerUser.subscriber_id == sub.id)
        .order_by(ResellerUser.created_at.desc())
    ).first()
    if ru is None:
        ru = ResellerUser(
            subscriber_id=sub.id,
            reseller_id=sub.reseller_id,
            is_active=True,
        )
        if apply:
            db.add(ru)
            db.flush()
    # Populate identity from the subscriber (idempotent).
    if apply:
        ru.email = ru.email or sub.email
        ru.full_name = ru.full_name or _full_name(sub)
        if ru.reseller_id is None:
            ru.reseller_id = sub.reseller_id
    return ru


def _apply(db) -> None:
    subs = db.scalars(
        select(Subscriber).where(Subscriber.user_type == UserType.reseller)
    ).all()
    print(f"Reseller subscribers to migrate: {len(subs)} (APPLY)")
    creds_moved = mfa_moved = 0
    for sub in subs:
        ru = _get_or_create_reseller_user(db, sub, apply=True)
        creds = db.scalars(
            select(UserCredential).where(UserCredential.subscriber_id == sub.id)
        ).all()
        for cred in creds:
            cred.subscriber_id = None
            cred.reseller_user_id = ru.id
            creds_moved += 1
        methods = db.scalars(
            select(MFAMethod).where(MFAMethod.subscriber_id == sub.id)
        ).all()
        for m in methods:
            m.subscriber_id = None
            m.reseller_user_id = ru.id
            mfa_moved += 1
        print(
            f"  {sub.email} (sub {sub.id}) -> reseller_user {ru.id}: "
            f"{len(creds)} cred(s), {len(methods)} mfa"
        )
    db.commit()
    print(f"Committed: {creds_moved} credential(s), {mfa_moved} mfa method(s).")


def _rollback(db) -> None:
    # Repoint reseller_user-principal credentials/MFA back to the subscriber via
    # the retained ResellerUser.subscriber_id linkage.
    creds = db.scalars(
        select(UserCredential).where(UserCredential.reseller_user_id.is_not(None))
    ).all()
    methods = db.scalars(
        select(MFAMethod).where(MFAMethod.reseller_user_id.is_not(None))
    ).all()
    print(
        f"Reverting {len(creds)} credential(s), {len(methods)} mfa method(s) (ROLLBACK)"
    )
    moved = 0
    for cred in creds:
        ru = db.get(ResellerUser, cred.reseller_user_id)
        if ru is None or ru.subscriber_id is None:
            print(f"  SKIP cred {cred.id}: no subscriber linkage on reseller_user")
            continue
        cred.reseller_user_id = None
        cred.subscriber_id = ru.subscriber_id
        moved += 1
    for m in methods:
        ru = db.get(ResellerUser, m.reseller_user_id)
        if ru is None or ru.subscriber_id is None:
            continue
        m.reseller_user_id = None
        m.subscriber_id = ru.subscriber_id
    db.commit()
    print(f"Reverted {moved} credential(s).")


def _dry_run(db) -> None:
    subs = db.scalars(
        select(Subscriber).where(Subscriber.user_type == UserType.reseller)
    ).all()
    print(f"Reseller subscribers to migrate: {len(subs)} (DRY-RUN)")
    for sub in subs:
        ru = db.scalars(
            select(ResellerUser).where(ResellerUser.subscriber_id == sub.id)
        ).first()
        creds = db.scalar(
            select(func.count(UserCredential.id)).where(
                UserCredential.subscriber_id == sub.id
            )
        )
        mfa = db.scalar(
            select(func.count(MFAMethod.id)).where(MFAMethod.subscriber_id == sub.id)
        )
        print(
            f"  {sub.email} (sub {sub.id}): reseller_user="
            f"{'exists ' + str(ru.id) if ru else 'CREATE'}, "
            f"{creds} cred(s), {mfa} mfa -> would repoint to reseller_user"
        )
    print("Re-run with --apply to write (coordinate with the flag flip).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="Repoint to reseller_user.")
    group.add_argument(
        "--rollback", action="store_true", help="Reverse: repoint back to subscriber."
    )
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
