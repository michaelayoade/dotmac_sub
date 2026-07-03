"""Backfill subscription bundles for existing base-internet + standalone-IP accounts.

For each subscriber that has a standalone IP subscription (SLASH */ PPPOE PUBLIC IP)
alongside a base internet subscription, create one SubscriptionBundle anchored on
the base internet sub and attach all non-terminal members. Also retires the
vestigial, unbilled IP add-on rows (the double-modeling) for those subscribers.

Dry-run by default. As a standalone script, run against prod only with RUN=1.
When called with a live session (e.g. tests), `commit=False` leaves the changes
uncommitted in the caller's session (it does NOT roll back the caller's work);
the caller owns the transaction boundary.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from sqlalchemy import or_, select

from app.models.catalog import (
    AddOn,
    AddOnType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.services import bundles

_IP_LIKE = ("%slash%", "%pppoe public ip%")
_NON_TERMINAL = (SubscriptionStatus.active, SubscriptionStatus.suspended)


def _is_ip(description: str | None) -> bool:
    d = (description or "").lower()
    return "slash" in d or "pppoe public ip" in d


def _retire_ip_addons(db, subscriber_id) -> int:
    rows = db.scalars(
        select(SubscriptionAddOn)
        .join(Subscription, Subscription.id == SubscriptionAddOn.subscription_id)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .where(
            Subscription.subscriber_id == subscriber_id,
            AddOn.addon_type.in_([AddOnType.extra_ip, AddOnType.static_ip]),
            SubscriptionAddOn.end_at.is_(None),
        )
    ).all()
    now = datetime.now(UTC)
    for row in rows:
        row.end_at = now
    db.flush()
    return len(rows)


def backfill_bundles(db, commit: bool = False) -> dict:
    ip_filter = or_(*[Subscription.service_description.ilike(p) for p in _IP_LIKE])
    subscriber_ids = db.scalars(
        select(Subscription.subscriber_id)
        .distinct()
        .where(
            ip_filter,
            Subscription.status.in_(_NON_TERMINAL),
            Subscription.bundle_id.is_(None),
        )
    ).all()
    created = members = deduped = 0
    for sid in subscriber_ids:
        subs = db.scalars(
            select(Subscription).where(
                Subscription.subscriber_id == sid,
                Subscription.status.in_(_NON_TERMINAL),
                Subscription.bundle_id.is_(None),
            )
        ).all()
        anchor = next((s for s in subs if not _is_ip(s.service_description)), None)
        if anchor is None:
            # No clear base internet service — skip (IP-only accounts).
            continue
        bundle = bundles.create_bundle(db, str(sid), str(anchor.id))
        for sub in subs:
            bundles.add_member(db, str(bundle.id), str(sub.id))
            members += 1
        created += 1
        deduped += _retire_ip_addons(db, sid)
    stats = {
        "bundles_created": created,
        "members_linked": members,
        "addons_retired": deduped,
    }
    if commit:
        db.commit()
    return stats


if __name__ == "__main__":  # pragma: no cover
    from app.tasks.collections import SessionLocal

    live = os.environ.get("RUN") == "1"
    db = SessionLocal()
    try:
        result = backfill_bundles(db, commit=live)
        if not live:
            db.rollback()  # dry-run: discard everything
        print(("COMMITTED " if live else "DRY-RUN ") + str(result))
    finally:
        db.close()
