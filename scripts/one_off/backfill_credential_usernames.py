"""Backfill ``user_credentials.username`` for local credentials missing one.

Prerequisite for the identity/email decoupling change (Layers 1+2): once login
resolution stops matching ``subscribers.email`` (auth_flow._resolve_login_credential),
any active local credential that relied on email-login and has no ``username``
would become unreachable. This script gives each such credential a stable,
unique username so no one is locked out.

Username source preference (username IS an identity, so it must stay unique —
unlike the now-non-unique contact email):
  1. subscriber.subscriber_number  (already globally unique)
  2. system_user.email             (admins; already unique)
  3. local part of the principal's email, de-duplicated with a numeric suffix

Run this and verify zero remaining NULL/empty-username active local credentials
BEFORE deploying the resolver change.

Dry-run by default; nothing is written without --apply.

Examples
--------
  # Audit what would change (read-only):
  python -m scripts.one_off.backfill_credential_usernames

  # Apply the backfill:
  python -m scripts.one_off.backfill_credential_usernames --apply
"""

from __future__ import annotations

import argparse
import re

from sqlalchemy import func, or_

from app.db import SessionLocal
from app.models.auth import AuthProvider, UserCredential
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def _slug(value: str) -> str:
    return _SLUG_RE.sub("", value.strip().lower()) or "user"


def _local_part(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return _slug(email.split("@", 1)[0])


def _candidate(db, cred: UserCredential) -> str | None:
    """Best stable username source for this credential's principal."""
    if cred.subscriber_id:
        sub = db.get(Subscriber, cred.subscriber_id)
        if sub is None:
            return None
        if sub.subscriber_number:
            return _slug(sub.subscriber_number)
        return _local_part(sub.email)
    if cred.system_user_id:
        su = db.get(SystemUser, cred.system_user_id)
        if su is None:
            return None
        return _slug(su.email) if su.email else None
    return None


def _unique(db, base: str, taken: set[str]) -> str:
    """Disambiguate against existing usernames and ones assigned this run."""
    if base not in taken and not _username_exists(db, base):
        taken.add(base)
        return base
    i = 2
    while True:
        candidate = f"{base}-{i}"
        if candidate not in taken and not _username_exists(db, candidate):
            taken.add(candidate)
            return candidate
        i += 1


def _username_exists(db, username: str) -> bool:
    return (
        db.query(UserCredential.id)
        .filter(func.lower(UserCredential.username) == username.lower())
        .first()
        is not None
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the backfilled usernames. Default: dry-run (report only).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        missing = (
            db.query(UserCredential)
            .filter(UserCredential.provider == AuthProvider.local)
            .filter(UserCredential.is_active.is_(True))
            .filter(
                or_(
                    UserCredential.username.is_(None),
                    func.trim(UserCredential.username) == "",
                )
            )
            .order_by(UserCredential.created_at.asc())
            .all()
        )

        print(
            f"Active local credentials missing a username: {len(missing)} "
            f"({'APPLY' if args.apply else 'DRY-RUN'})"
        )
        taken: set[str] = set()
        assigned = 0
        skipped = 0
        for cred in missing:
            base = _candidate(db, cred)
            if not base:
                skipped += 1
                print(f"  SKIP credential {cred.id}: no username source available")
                continue
            username = _unique(db, base, taken)
            assigned += 1
            print(f"  {cred.id} -> {username}")
            if args.apply:
                cred.username = username
                db.add(cred)

        if args.apply:
            db.commit()
            print(f"Committed {assigned} username(s); {skipped} skipped.")
        else:
            print(
                f"Would assign {assigned} username(s); {skipped} skipped. "
                "Re-run with --apply to write."
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
