#!/usr/bin/env python
"""Make QA/test subscriptions terminal when their subscriber is terminal.

Data-consistency cleanup (NOT a billing-launch step). A QA/test login
(e.g. ``qa-test-*``) left as an *active* subscription on a *canceled* subscriber
inflates the launch-gate audit population. This sets ONLY those QA subscriptions
to ``canceled`` so the subscriber/subscription states agree.

Strictly scoped: only subscriptions whose login matches a QA/test prefix AND
whose subscriber is terminal. Never touches a real customer or a QA sub on an
active subscriber. Dry-run by default.

Usage:
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/fix_qa_active_on_terminal_subscriber.py          # dry-run
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/fix_qa_active_on_terminal_subscriber.py --apply
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from sqlalchemy import select

from app.db import SessionLocal
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberStatus

QA_PREFIXES = ("qa", "test", "e2e", "demo")
_TERMINAL_SUBSCRIBER = (SubscriberStatus.canceled, SubscriberStatus.disabled)


def _is_qa(login: str | None) -> bool:
    return bool(login) and login.strip().lower().startswith(QA_PREFIXES)


def find_qa_active_on_terminal(db) -> list[Subscription]:
    """Active subscriptions with a QA/test login whose subscriber is terminal."""
    rows = db.scalars(
        select(Subscription)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .where(Subscription.status == SubscriptionStatus.active)
        .where(Subscriber.status.in_(_TERMINAL_SUBSCRIBER))
    ).all()
    return [s for s in rows if _is_qa(s.login)]


def main(execute: bool) -> int:
    db = SessionLocal()
    try:
        targets = find_qa_active_on_terminal(db)
        print(f"QA active-on-terminal subscriptions: {len(targets)}")
        for s in targets:
            print(f"  sub={s.id} login={s.login} -> canceled")
        if not execute:
            print("\nDRY RUN — nothing changed. Re-run with --apply.")
            return 0
        from app.services.account_lifecycle import compute_account_status

        now = datetime.now(UTC)
        for s in targets:
            s.status = SubscriptionStatus.canceled
            if s.canceled_at is None:
                s.canceled_at = now
        db.flush()
        for sid in {s.subscriber_id for s in targets}:
            compute_account_status(db, str(sid))
        db.commit()
        print(f"\nAPPLIED — canceled {len(targets)} QA subscriptions.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(execute="--apply" in sys.argv))
