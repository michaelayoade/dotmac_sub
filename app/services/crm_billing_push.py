"""Nightly billing snapshot push (Sub → DotMac Omni CRM).

CRM agents see balance / next bill date / billing cycle on the subscriber
record, but those columns were never populated — support quoted stale or
empty billing info. This pushes a small snapshot to every CRM-linked
subscriber, skipping ones whose snapshot hasn't changed since the last push
(stored in subscriber metadata) so steady-state nights are mostly no-ops.

Delivery goes through the CRM's sync webhook, NOT the subscriber PATCH
endpoint: the CRM's SubscriberUpdate schema only accepts person/org/status/
notes and silently drops billing fields (verified live — 200 with no
effect), while the webhook upsert applies any Subscriber column. Splynx-
linked subscribers use the splynx-shaped payload (its mapper reads balance/
currency/next_bill_date; it has no billing_cycle output); natives use the
generic dotmac payload with CRM column names.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber

logger = logging.getLogger(__name__)

_SNAPSHOT_KEY = "crm_billing_snapshot"


def build_snapshot(db: Session, subscriber: Subscriber) -> dict[str, Any]:
    """CRM Subscriber billing columns for one local subscriber."""
    from app.services.billing._common import get_account_credit_balance

    balance = get_account_credit_balance(db, str(subscriber.id))
    next_bill: datetime | None = (
        db.query(Subscription.next_billing_at)
        .filter(
            Subscription.subscriber_id == subscriber.id,
            Subscription.status == SubscriptionStatus.active,
            Subscription.next_billing_at.isnot(None),
        )
        .order_by(Subscription.next_billing_at.asc())
        .limit(1)
        .scalar()
    )
    billing_mode = getattr(subscriber.billing_mode, "value", subscriber.billing_mode)
    return {
        "balance": f"{balance:.2f}",
        "currency": os.getenv("BILLING_DEFAULT_CURRENCY", "NGN"),
        "billing_cycle": str(billing_mode or "") or None,
        "next_bill_date": next_bill.isoformat() if next_bill else None,
    }


def push_billing_snapshots(
    db: Session,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Enqueue changed billing snapshots to all CRM-linked subscribers.

    Each changed snapshot is dispatched through the retrying push task
    (app.tasks.crm_sync.push_subscriber_change) rather than pushed inline:
    a slow/unreachable CRM no longer blocks or silently drops the batch, and
    terminal failures land in the dead-letter (crm_sync_failures). The task
    stamps the snapshot key on SUCCESS, so a record we couldn't deliver stays
    unstamped and is naturally re-enqueued next run (auto-heal).
    """
    from app.services.crm_webhook import NATIVE_EXTERNAL_SYSTEM
    from app.tasks.crm_sync import push_subscriber_change as push_task

    stats = {"considered": 0, "enqueued": 0, "unchanged": 0}

    query = (
        db.query(Subscriber)
        .filter(
            Subscriber.crm_subscriber_id.isnot(None),
            Subscriber.is_active.is_(True),
        )
        .order_by(Subscriber.id)
    )
    if limit:
        query = query.limit(limit)

    for subscriber in query.all():
        stats["considered"] += 1
        snapshot = build_snapshot(db, subscriber)
        sendable = {k: v for k, v in snapshot.items() if v is not None}
        if subscriber.splynx_customer_id:
            external_id: int | str = subscriber.splynx_customer_id
            external_system = "splynx"
            # The splynx mapper has no billing_cycle output.
            payload = {k: v for k, v in sendable.items() if k != "billing_cycle"}
        else:
            external_id = str(subscriber.id)
            external_system = NATIVE_EXTERNAL_SYSTEM
            payload = sendable
        # Dedupe on exactly what we transmit, which is what the task stamps.
        metadata = dict(subscriber.metadata_ or {})
        if metadata.get(_SNAPSHOT_KEY) == payload:
            stats["unchanged"] += 1
            continue
        push_task.delay(
            external_id,
            payload,
            external_system,
            billing_snapshot_subscriber_id=str(subscriber.id),
        )
        stats["enqueued"] += 1

    return stats
