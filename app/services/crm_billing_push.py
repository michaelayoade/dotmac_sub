"""Nightly billing snapshot push (Sub → DotMac Omni CRM).

CRM agents see balance / next bill date / billing cycle on the subscriber
record, but those columns were never populated — support quoted stale or
empty billing info. This pushes a small snapshot to every CRM-linked
subscriber, skipping ones whose snapshot hasn't changed since the last push
(stored in subscriber metadata) so steady-state nights are mostly no-ops.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber
from app.services.crm_client import CRMClient, CRMClientError, get_crm_client

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
    client: CRMClient | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Push changed billing snapshots to all CRM-linked subscribers."""
    client = client or get_crm_client()
    stats = {"considered": 0, "pushed": 0, "unchanged": 0, "failed": 0}

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
        metadata = dict(subscriber.metadata_ or {})
        if metadata.get(_SNAPSHOT_KEY) == sendable:
            stats["unchanged"] += 1
            continue
        try:
            client.update_subscriber(str(subscriber.crm_subscriber_id), sendable)
        except CRMClientError as exc:
            stats["failed"] += 1
            logger.warning(
                "CRM billing push failed subscriber=%s: %s", subscriber.id, exc
            )
            continue
        metadata[_SNAPSHOT_KEY] = sendable
        subscriber.metadata_ = metadata
        db.commit()
        stats["pushed"] += 1

    return stats
