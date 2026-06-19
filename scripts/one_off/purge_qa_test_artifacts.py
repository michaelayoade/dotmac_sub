"""Safe teardown of synthetic QA / Playwright / e2e test-artifact customers.

These are machine-generated accounts left in prod by automated test runs. They
are identified ONLY by reserved test-domain / harness email patterns
(@example.com, @example.invalid with qa./e2e./pppoe-ui-/playwright-/codex.test
shapes). Real customers whose human *name* merely contains "test" are NOT
matched, and WhatsApp-onboarded customers (whatsapp--<phone>@example.invalid),
the reseller server account, and the johndoe stub are explicitly EXCLUDED.

What it does per matched subscriber (idempotent, reversible soft-delete):
  1. cancel_subscription() on every non-terminal subscription
     -> sets status=canceled, releases IPv4/IPv6 assignments back to the pool,
        clears subscription.ipv4_address/ipv6_address, emits the cancel event,
        recomputes account status.
  2. Deactivates internal radius_users rows for the subscriber (is_active=False).
  3. compute_account_status() to sync subscriber status/is_active.
  4. Forces the subscriber terminal: status=canceled, is_active=False, and stamps
     metadata_ with a purge marker so the action is auditable and re-runs are no-ops.

It does NOT hard-delete (the app never does) and does NOT SSH/CoA the NAS
(footprint shows 0 live sessions; pass --kick to also disconnect live sessions
if that ever changes).

DRY-RUN by default. Use --apply to commit.

    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/purge_qa_test_artifacts.py          # preview
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/purge_qa_test_artifacts.py --apply   # execute
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.db import SessionLocal
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.account_lifecycle import cancel_subscription, compute_account_status
from app.services.ip_lifecycle import release_service_ips_for_subscription

# Exact-match patterns for the confirmed synthetic artifacts. Anchored so a real
# customer cannot accidentally match. Keep this list as the single source of truth.
INCLUDE_RE = re.compile(
    r"^("
    r"qa\.[a-z]+\.\d+@example\.com"          # 2026-06-07 QA edge-case sweep batch
    r"|e2e\.(user|agent)@example\.com"        # 2026-03-21 e2e seed
    r"|admin@example\.com"                    # 2026-03-21 e2e seed (subscriber, not the real SystemUser)
    r"|pppoe-ui-\d+@example\.com"             # 2026-03-21 PPPoE UI-verify harness
    r"|qa\.test(reseller|customer)@example\.invalid"  # 2026-05-24 QA pair
    r"|playwright-admin@example\.com"         # explicit Playwright
    r"|codex\.test\+\d+@example\.com"         # agent-generated
    r")$"
)

# Belt-and-suspenders: these reserved-domain rows are REAL data and must never
# be touched even if a future regex edit would catch them.
EXCLUDE_EMAILS = {
    "johndoe@example.com",
    "wanserver.reseller.20260618@example.com",
}
EXCLUDE_RE = re.compile(r"^whatsapp--\d+@example\.invalid$")

MAX_AFFECTED = 30  # runaway guard: refuse to act if the set is unexpectedly large

PURGE_REASON = "qa_test_artifact_purge"
PURGE_SOURCE = "purge_qa_test_artifacts"

TERMINAL_SUB_STATUSES = {"canceled", "expired", "disabled"}


def _select_artifacts(db) -> list[Subscriber]:
    rows = (
        db.query(Subscriber)
        .filter(Subscriber.email.isnot(None))
        .filter(Subscriber.email.ilike("%@example.%"))
        .all()
    )
    out = []
    for s in rows:
        email = (s.email or "").lower()
        if email in EXCLUDE_EMAILS or EXCLUDE_RE.match(email):
            continue
        if INCLUDE_RE.match(email):
            out.append(s)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit changes (default: dry-run)")
    ap.add_argument("--kick", action="store_true", help="also CoA/disconnect any live sessions")
    args = ap.parse_args()

    db_url = settings.database_url
    db_name = db_url.rsplit("/", 1)[-1].split("?")[0]
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] target DB: {db_name}")

    db = SessionLocal()
    try:
        subs_subjects = _select_artifacts(db)
        n = len(subs_subjects)
        print(f"Matched {n} synthetic test-artifact subscriber(s).\n")
        if n == 0:
            print("Nothing to do.")
            return 0
        if n > MAX_AFFECTED:
            print(f"ABORT: matched {n} > MAX_AFFECTED ({MAX_AFFECTED}). Refusing to run; "
                  "review the include regex.")
            return 2

        now = datetime.now(UTC)
        sessions_kicked = 0

        for s in subs_subjects:
            sid = str(s.id)
            subscriptions = list(s.subscriptions)

            sub_descr = []
            for sub in subscriptions:
                sub_id = str(sub.id)
                status = str(sub.status).split(".")[-1].lower()
                terminal = status in TERMINAL_SUB_STATUSES
                action = " ->release-ips" if terminal else " ->cancel"
                sub_descr.append(f"{sub_id[:8]}={status}{action}")
                if args.apply:
                    if not terminal:
                        # Sets canceled + releases IPv4/IPv6 + clears cache cols + emits.
                        cancel_subscription(
                            db, sub_id, cancel_reason=PURGE_REASON, source=PURGE_SOURCE, emit=True
                        )
                        if args.kick:
                            from app.services.enforcement import disconnect_subscription_sessions
                            sessions_kicked += disconnect_subscription_sessions(
                                db, sub_id, reason=PURGE_REASON
                            )
                    else:
                        # Already terminal but may still hold stale active IP assignments.
                        release_service_ips_for_subscription(db, sub)

            print(f"  {s.status!s:>10} act={s.is_active} | {s.email} | subs[{len(subscriptions)}]: "
                  f"{', '.join(sub_descr) or '-'}")

            if args.apply:
                # Deactivate internal radius_users mirror for this subscriber.
                db.execute(
                    text("UPDATE radius_users SET is_active=false "
                         "WHERE subscriber_id=:sid AND is_active=true"),
                    {"sid": sid},
                )
                # Catch-all: release any straggler active IP assignments not tied to a
                # current subscription (orphaned by earlier partial cleanups).
                db.execute(
                    text("UPDATE ip_assignments SET is_active=false "
                         "WHERE subscriber_id=:sid AND is_active=true"),
                    {"sid": sid},
                )
                db.flush()
                compute_account_status(db, sid)
                db.flush()
                # Force terminal soft-delete + audit marker (covers 0-subscription rows).
                db.refresh(s)
                if s.status != SubscriberStatus.canceled or s.is_active:
                    s.status = SubscriberStatus.canceled
                    s.is_active = False
                meta = dict(s.metadata_ or {})
                meta["qa_artifact_purged_at"] = now.isoformat()
                meta["qa_artifact_purged_by"] = PURGE_SOURCE
                s.metadata_ = meta
                flag_modified(s, "metadata_")
                db.flush()

        if args.apply:
            db.commit()
            print(f"\nAPPLIED. {n} subscriber(s) soft-deleted; live sessions kicked: {sessions_kicked}.")
        else:
            print(f"\nDRY-RUN complete. {n} subscriber(s) would be soft-deleted. Re-run with --apply.")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
