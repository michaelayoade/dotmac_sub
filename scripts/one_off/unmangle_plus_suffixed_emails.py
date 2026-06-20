"""Un-mangle ``localpart+NNNN@domain`` contact emails back to the real address.

During the Splynx import / pre-decoupling era, customer contact emails were
artificially suffixed (``wanserverng+8265@gmail.com``, ``+8266``, ``+8267``, …)
purely to satisfy the old global ``UNIQUE(subscribers.email)`` constraint when
many customers under one reseller legitimately shared a contact address. Those
``+NNNN`` tags are not the customer's real email.

Now that email is non-unique contact info (see the identity/email decoupling
change — ``docs/designs/IDENTITY_EMAIL_DECOUPLING.md`` / migration
``162_subscriber_email_non_unique``), this restores the real address by
stripping the generated numeric ``+NNNN`` tag. It does NOT merge customer
records — distinct customers keep distinct rows and are simply allowed to share
the same contact email, which is the whole point.

PREREQUISITE: the email column must already be non-unique (mig 162 deployed).
Un-mangling deliberately creates duplicate emails; if the unique constraint is
still present this script refuses in --apply mode (the UPDATE would fail anyway).

Safety heuristic (default ON): only un-mangle a ``+NNNN`` address when its
stripped form is corroborated — i.e. it clusters with at least one sibling
(another ``+NNNN`` variant or an existing plain record sharing the stripped
address). This avoids touching a customer's *legitimate* lone plus-tag like
``jane+2024@gmail.com``. Use --no-require-cluster to also strip lone tags.

Dry-run by default; nothing is written without --apply.

Examples
--------
  # Review what would change (read-only), corroborated tags only:
  python -m scripts.one_off.unmangle_plus_suffixed_emails

  # Include lone (un-clustered) +NNNN tags in the report:
  python -m scripts.one_off.unmangle_plus_suffixed_emails --no-require-cluster

  # Apply (requires mig 162 deployed):
  python -m scripts.one_off.unmangle_plus_suffixed_emails --apply
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict

from sqlalchemy import func, inspect

from app.db import SessionLocal
from app.models.subscriber import Subscriber

# localpart + one-or-more digits, then @domain. The numeric tag is the tell-tale
# of the uniqueness-dodging generator (legitimate tags are usually words).
_MANGLED_RE = re.compile(r"^(?P<local>[^+@\s]+)\+(?P<tag>\d+)@(?P<domain>[^@\s]+)$")


def _real_email(email: str) -> str | None:
    m = _MANGLED_RE.match(email.strip())
    if not m:
        return None
    return f"{m.group('local')}@{m.group('domain')}".lower()


def _email_is_unique_constrained(db) -> bool:
    inspector = inspect(db.get_bind())
    for uc in inspector.get_unique_constraints("subscribers"):
        if uc.get("column_names") == ["email"]:
            return True
    for ix in inspector.get_indexes("subscribers"):
        if ix.get("unique") and ix.get("column_names") == ["email"]:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the restored emails. Default: dry-run (report only).",
    )
    parser.add_argument(
        "--no-require-cluster",
        dest="require_cluster",
        action="store_false",
        help=(
            "Also strip lone +NNNN tags that have no sibling/plain match. "
            "Default: only un-mangle corroborated (clustered) tags."
        ),
    )
    parser.set_defaults(require_cluster=True)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.apply and _email_is_unique_constrained(db):
            print(
                "REFUSING: subscribers.email still has a UNIQUE constraint. "
                "Deploy migration 162 (email non-unique) first — un-mangling "
                "creates intentional duplicates that UNIQUE would reject."
            )
            return

        # Candidate mangled rows.
        candidates = (
            db.query(Subscriber)
            .filter(Subscriber.email.op("~")(r"\+[0-9]+@"))
            .all()
        )
        # Postgres regex above pre-filters; re-validate in Python for portability
        # and to compute the stripped address.
        parsed: list[tuple[Subscriber, str]] = []
        for sub in candidates:
            real = _real_email(sub.email or "")
            if real:
                parsed.append((sub, real))

        # Cluster by stripped address, and count existing plain records.
        by_real: dict[str, list[Subscriber]] = defaultdict(list)
        for sub, real in parsed:
            by_real[real].append(sub)

        plain_counts: dict[str, int] = {}
        for real in by_real:
            plain_counts[real] = (
                db.query(func.count(Subscriber.id))
                .filter(func.lower(Subscriber.email) == real)
                .scalar()
                or 0
            )

        print(
            f"Mangled (+NNNN) contact emails found: {len(parsed)} "
            f"across {len(by_real)} real addresses "
            f"({'APPLY' if args.apply else 'DRY-RUN'}, "
            f"require_cluster={args.require_cluster})"
        )

        changed = 0
        skipped_lone = 0
        for real, subs in sorted(by_real.items()):
            corroborated = len(subs) >= 2 or plain_counts.get(real, 0) > 0
            eligible = corroborated or not args.require_cluster
            tag = "" if eligible else "  [SKIP lone — use --no-require-cluster]"
            print(
                f"  {real}  <- {len(subs)} mangled"
                f"{', + ' + str(plain_counts[real]) + ' plain' if plain_counts.get(real) else ''}"
                f"{tag}"
            )
            if not eligible:
                skipped_lone += len(subs)
                continue
            for sub in subs:
                print(f"      {sub.id}: {sub.email} -> {real}")
                changed += 1
                if args.apply:
                    sub.email = real
                    db.add(sub)

        if args.apply:
            db.commit()
            print(f"Committed {changed} email(s); {skipped_lone} lone tag(s) skipped.")
        else:
            print(
                f"Would restore {changed} email(s); {skipped_lone} lone tag(s) "
                "skipped. Re-run with --apply (after mig 162) to write."
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
