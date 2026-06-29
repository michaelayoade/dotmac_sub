"""One-time push of native subscribers into the CRM.

Creates each native subscriber in the CRM via the generic sync webhook
(external_system=dotmac, external_id=<local subscriber UUID>) and stores the
returned CRM subscriber UUID in subscribers.crm_subscriber_id. Going forward
the subscriber_created event does this automatically; this covers the ones
created before that wiring existed.

Idempotent: subscribers that already have crm_subscriber_id are skipped, and
the CRM upserts by (external_system, external_id) so re-runs don't duplicate.
"""

from __future__ import annotations

import argparse
import logging
from uuid import UUID

from app.db import SessionLocal
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber
from app.services.crm_webhook import (
    NATIVE_EXTERNAL_SYSTEM,
    native_subscriber_payload,
    push_subscriber_change,
)

logger = logging.getLogger(__name__)


def _primary_service(db, subscriber: Subscriber) -> tuple[str, str]:
    subscription = (
        db.query(Subscription)
        .filter(
            Subscription.subscriber_id == subscriber.id,
            Subscription.status == SubscriptionStatus.active,
        )
        .first()
    )
    if not subscription or not subscription.offer:
        return "", ""
    offer = subscription.offer
    speed = ""
    if offer.speed_download_mbps and offer.speed_upload_mbps:
        speed = f"{offer.speed_download_mbps}/{offer.speed_upload_mbps} Mbps"
    return offer.name, speed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only")
    parser.add_argument(
        "--include-inactive", action="store_true", help="also push inactive accounts"
    )
    args = parser.parse_args()

    db = SessionLocal()
    stats = {"pushed": 0, "linked": 0, "skipped_linked": 0, "failed": 0}
    try:
        query = db.query(Subscriber).filter(Subscriber.splynx_customer_id.is_(None))
        if not args.include_inactive:
            query = query.filter(Subscriber.is_active.is_(True))
        for subscriber in query.all():
            if subscriber.crm_subscriber_id:
                stats["skipped_linked"] += 1
                continue
            service_name, service_speed = _primary_service(db, subscriber)
            payload = native_subscriber_payload(
                subscriber, service_name=service_name, service_speed=service_speed
            )
            if args.dry_run:
                print(f"[dry-run] would push {subscriber.id}: {payload}")
                stats["pushed"] += 1
                continue
            crm_id = push_subscriber_change(
                str(subscriber.id), payload, NATIVE_EXTERNAL_SYSTEM
            )
            if not crm_id:
                stats["failed"] += 1
                logger.warning("push failed for subscriber %s", subscriber.id)
                continue
            stats["pushed"] += 1
            try:
                subscriber.crm_subscriber_id = UUID(crm_id)
                stats["linked"] += 1
            except (TypeError, ValueError):
                logger.warning(
                    "CRM returned no usable id for %s: %r", subscriber.id, crm_id
                )
        if not args.dry_run:
            db.commit()
    finally:
        db.close()
    print(stats)


if __name__ == "__main__":
    main()
