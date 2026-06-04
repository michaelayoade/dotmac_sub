"""Discover NEW Splynx services and create/update matching dotmac Subscription rows.

For each Splynx services_internet row (active/blocked, deleted='0') where dotmac
either has no Subscription with that splynx_service_id:
  - If there's an EXISTING active dotmac sub with the same (login, subscriber_id):
    Splynx renewed/replaced the service — UPDATE that sub's splynx_service_id,
    offer_id, and ipv4_address.
  - Otherwise: INSERT a new Subscription.

Does NOT create AccessCredential — run bootstrap_radius_from_splynx --missing-only
afterward to fill credentials for newly-created subs.
"""

from __future__ import annotations

import argparse
import logging
import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select, update

from app.db import SessionLocal
from app.models.catalog import (
    BillingMode,
    ContractTerm,
    Subscription,
    SubscriptionStatus,
)
from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping
from app.models.subscriber import Subscriber
from app.services.migrations.db_connections import splynx_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STATUS_MAP = {
    "active": SubscriptionStatus.active,
    "disabled": SubscriptionStatus.disabled,
    "stopped": SubscriptionStatus.stopped,
    "hidden": SubscriptionStatus.hidden,
    "pending": SubscriptionStatus.pending,
    "blocked": SubscriptionStatus.blocked,
    "new": SubscriptionStatus.pending,
}


def run(dry_run: bool = True) -> dict[str, int]:
    stats: Counter = Counter()
    db = SessionLocal()
    try:
        existing = {
            sid
            for (sid,) in db.execute(
                select(Subscription.splynx_service_id).where(
                    Subscription.splynx_service_id.isnot(None)
                )
            ).all()
        }
        logger.info("existing dotmac subs with splynx_service_id: %d", len(existing))

        with splynx_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, customer_id, login, ipv4, status, tariff_id, "
                    "start_date FROM services_internet "
                    "WHERE deleted='0' AND status IN ('active','blocked','pending')"
                )
                rows = cur.fetchall()
        candidates = [r for r in rows if r["id"] not in existing]
        stats["splynx_total_undeleted_services"] = len(rows)
        stats["already_in_dotmac"] = len(rows) - len(candidates)
        stats["new_to_process"] = len(candidates)
        logger.info(
            "Splynx services undeleted: %d. Already in dotmac: %d. New: %d",
            len(rows),
            len(rows) - len(candidates),
            len(candidates),
        )
        if not candidates:
            return dict(stats)

        sub_by_splynx = {
            s.splynx_customer_id: s.id
            for s in db.scalars(
                select(Subscriber).where(Subscriber.splynx_customer_id.isnot(None))
            ).all()
        }
        tariff_to_offer = {
            m.splynx_id: m.dotmac_id
            for m in db.scalars(
                select(SplynxIdMapping).where(
                    SplynxIdMapping.entity_type == SplynxEntityType.tariff
                )
            ).all()
        }

        to_insert: list[dict] = []
        to_update: list[dict] = []
        for svc in candidates:
            subscriber_id = sub_by_splynx.get(svc["customer_id"])
            if subscriber_id is None:
                stats["skipped_no_subscriber"] += 1
                continue
            offer_id = tariff_to_offer.get(svc["tariff_id"])
            if offer_id is None:
                stats["skipped_no_offer_mapping"] += 1
                continue
            target_status = STATUS_MAP.get(
                (svc["status"] or "active").strip().lower(),
                SubscriptionStatus.active,
            )
            login = (svc["login"] or "").strip() or None
            ipv4 = (svc["ipv4"] or "").strip() or None
            start_at = svc.get("start_date") or datetime.now(UTC)

            # If active+login: check for collision with the partial unique index
            existing_sub = None
            if login and target_status == SubscriptionStatus.active:
                existing_sub = db.execute(
                    select(Subscription).where(
                        Subscription.login == login,
                        Subscription.subscriber_id == subscriber_id,
                        Subscription.status == SubscriptionStatus.active,
                    )
                ).scalar()

            rec = {
                "splynx_id": svc["id"],
                "subscriber_id": subscriber_id,
                "offer_id": offer_id,
                "status": target_status,
                "login": login,
                "ipv4_address": ipv4,
                "start_at": start_at,
                "service_status_raw": svc["status"],
            }
            if existing_sub is not None:
                rec["existing_sub_id"] = existing_sub.id
                rec["old_splynx_id"] = existing_sub.splynx_service_id
                to_update.append(rec)
            else:
                to_insert.append(rec)

        stats["will_insert"] = len(to_insert)
        stats["will_update"] = len(to_update)
        logger.info("plan: INSERT=%d UPDATE=%d", len(to_insert), len(to_update))
        for k, v in stats.most_common():
            logger.info("  %s: %d", k, v)

        if dry_run:
            for rec in to_insert[:5]:
                logger.info(
                    "  would-INSERT: splynx_id=%s login=%s ipv4=%s",
                    rec["splynx_id"],
                    rec["login"],
                    rec["ipv4_address"],
                )
            for rec in to_update[:5]:
                logger.info(
                    "  would-UPDATE: sub %s old_splynx=%s -> new_splynx=%s login=%s",
                    str(rec["existing_sub_id"])[:8],
                    rec["old_splynx_id"],
                    rec["splynx_id"],
                    rec["login"],
                )
            logger.info("DRY RUN — pass --execute to apply")
            return dict(stats)

        now = datetime.now(UTC)
        for rec in to_insert:
            sub = Subscription(
                id=uuid.uuid4(),
                subscriber_id=rec["subscriber_id"],
                offer_id=rec["offer_id"],
                status=rec["status"],
                billing_mode=BillingMode.prepaid,
                contract_term=ContractTerm.month_to_month,
                login=rec["login"],
                ipv4_address=rec["ipv4_address"],
                splynx_service_id=rec["splynx_id"],
                start_at=rec["start_at"],
                service_status_raw=rec["service_status_raw"],
                created_at=now,
                updated_at=now,
            )
            db.add(sub)
            stats["inserted"] += 1
        for rec in to_update:
            db.execute(
                update(Subscription)
                .where(Subscription.id == rec["existing_sub_id"])
                .values(
                    splynx_service_id=rec["splynx_id"],
                    offer_id=rec["offer_id"],
                    ipv4_address=rec["ipv4_address"],
                    service_status_raw=rec["service_status_raw"],
                    updated_at=now,
                )
            )
            stats["updated"] += 1
        db.commit()
        logger.info(
            "applied: %d INSERT, %d UPDATE", stats["inserted"], stats["updated"]
        )
    finally:
        db.close()
    return dict(stats)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    run(dry_run=not args.execute)
