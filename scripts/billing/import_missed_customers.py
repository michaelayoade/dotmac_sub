"""Import genuine Splynx customers missed by the original migration.

The phase-1 migration excluded leads (and some customers were added to Splynx
afterwards). This imports the *real* missed customers — NOT lead, NOT deleted,
and either person-category or having transactions/services — as local
subscribers, then links their mirrored transactions (subscriber_id backfill).

Faithful to the migration's field mapping (name split, email dedup, status,
billing_mode from customers.billing_type, deposit + billing fields). Leads and
deleted customers are deliberately skipped. Their services are NOT created here
— the new-subscriptions sync picks those up once the subscriber exists.

Dry-run by default.

Usage:
    python scripts/billing/import_missed_customers.py            # dry-run
    python scripts/billing/import_missed_customers.py --execute
"""

from __future__ import annotations

import sys
import uuid
from decimal import Decimal

from app.db import SessionLocal
from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping
from app.models.splynx_transaction import SplynxBillingTransaction
from app.models.subscriber import Reseller, Subscriber, UserType
from app.services.migrations.billing_modes import map_billing_mode
from scripts.migration.db_connections import fetch_all, splynx_connection
from scripts.migration.phase1_customers_services import (
    _dedup_email,
    _map_customer_status,
    _parse_date,
    _split_name,
)


def _real_missed(db, conn) -> list[dict]:
    local = {
        int(c)
        for (c,) in db.query(Subscriber.splynx_customer_id).filter(
            Subscriber.splynx_customer_id.isnot(None)
        )
    }
    allc = fetch_all(
        conn,
        "SELECT id, name, email, phone, street_1, city, zip_code, category, "
        "status, deleted, billing_type, date_add, partner_id FROM customers",
    )
    txn = {
        int(r["customer_id"])
        for r in fetch_all(
            conn, "SELECT DISTINCT customer_id FROM billing_transactions"
        )
    }
    svc = {
        int(r["customer_id"])
        for r in fetch_all(
            conn, "SELECT DISTINCT customer_id FROM services_internet WHERE deleted='0'"
        )
    }
    out = []
    for r in allc:
        cid = int(r["id"])
        if cid in local or r["category"] == "lead" or r["deleted"] == "1":
            continue
        if r["category"] == "person" or cid in txn or cid in svc:
            out.append(r)
    return out


def main(execute: bool) -> None:
    db = SessionLocal()
    try:
        with splynx_connection() as conn:
            missed = _real_missed(db, conn)
            cids = [int(r["id"]) for r in missed]
            billing = {}
            if cids:
                ph = ",".join(["%s"] * len(cids))
                bquery = (
                    "SELECT customer_id, deposit, billing_date, billing_due, "  # noqa: S608
                    "grace_period, enabled FROM customer_billing "
                    f"WHERE customer_id IN ({ph})"
                )
                billing = {
                    int(b["customer_id"]): b
                    for b in fetch_all(conn, bquery, tuple(cids))
                }

        print(f"real missed customers to import: {len(missed)}")
        for r in missed:
            print(
                f"  {r['id']}: {r['name'][:34]!r} cat={r['category']} status={r['status']} mode={map_billing_mode(r['billing_type']).value}"
            )

        if not missed:
            print("nothing to import.")
            return
        if not execute:
            print("\nDRY-RUN — nothing written. Re-run with --execute to import.")
            return

        # reseller_id is NOT NULL: resolve via the customer's Splynx partner,
        # falling back to the house/direct reseller (SPL-1 "Main").
        partner_map = {
            int(m.splynx_id): m.dotmac_id
            for m in db.query(SplynxIdMapping).filter(
                SplynxIdMapping.entity_type == SplynxEntityType.partner
            )
        }
        default_reseller = db.query(Reseller).filter(Reseller.code == "SPL-1").first()
        if default_reseller is None:
            raise SystemExit("no default reseller (SPL-1) found — aborting")

        seen_emails = {
            e.lower()
            for (e,) in db.query(Subscriber.email).filter(Subscriber.email.isnot(None))
        }
        seen_nums = {
            n
            for (n,) in db.query(Subscriber.subscriber_number).filter(
                Subscriber.subscriber_number.isnot(None)
            )
        }
        created = 0
        linked_existing = 0
        new_subs: dict[int, uuid.UUID] = {}
        for r in missed:
            cid = int(r["id"])
            # A customer can already have a mapping (e.g. a Splynx duplicate of an
            # imported customer). Don't create a second subscriber — link this
            # customer's transactions to the already-mapped subscriber instead.
            existing_map = (
                db.query(SplynxIdMapping)
                .filter(
                    SplynxIdMapping.entity_type == SplynxEntityType.customer,
                    SplynxIdMapping.splynx_id == cid,
                )
                .first()
            )
            if existing_map is not None:
                new_subs[cid] = existing_map.dotmac_id
                linked_existing += 1
                continue
            b = billing.get(cid, {})
            first, last = _split_name(r["name"] or "")
            email = _dedup_email(r["email"] or "", cid, seen_emails)
            pid = r.get("partner_id")
            reseller_id = partner_map.get(int(pid)) if pid else None
            reseller_id = reseller_id or default_reseller.id
            sub_number = str(cid).zfill(6)
            while sub_number in seen_nums:
                sub_number = f"{sub_number}-{cid}"
            seen_nums.add(sub_number)
            sub = Subscriber(
                id=uuid.uuid4(),
                first_name=first,
                last_name=last,
                display_name=(r["name"] or "")[:120] or first,
                email=email,
                phone=(r.get("phone") or "")[:40] or None,
                address_line1=(r.get("street_1") or "")[:120] or None,
                city=(r.get("city") or "")[:80] or None,
                postal_code=(r.get("zip_code") or "")[:20] or None,
                country_code="NG",
                subscriber_number=sub_number,
                account_number=str(cid),
                account_start_date=_parse_date(r.get("date_add")),
                status=_map_customer_status(r["status"], is_deleted=False),
                user_type=UserType.customer,
                is_active=True,
                reseller_id=reseller_id,
                billing_enabled=b.get("enabled") in (1, "1", True),
                billing_mode=map_billing_mode(r["billing_type"]),
                billing_day=b.get("billing_date"),
                payment_due_days=b.get("billing_due"),
                grace_period_days=b.get("grace_period"),
                deposit=Decimal(str(b.get("deposit") or "0")),
                splynx_customer_id=cid,
                metadata_={
                    "imported_by": "import_missed_customers",
                    "splynx_status": r["status"],
                },
            )
            db.add(sub)
            db.flush()
            db.add(
                SplynxIdMapping(
                    entity_type=SplynxEntityType.customer,
                    splynx_id=cid,
                    dotmac_id=sub.id,
                )
            )
            new_subs[cid] = sub.id
            created += 1

        # Link their mirrored transactions to the new subscribers.
        linked = 0
        for cid, sid in new_subs.items():
            linked += (
                db.query(SplynxBillingTransaction)
                .filter(SplynxBillingTransaction.splynx_customer_id == cid)
                .update(
                    {SplynxBillingTransaction.subscriber_id: sid},
                    synchronize_session=False,
                )
            )
        db.commit()
        print(
            f"\nDONE — created {created} new subscribers, "
            f"linked {linked_existing} duplicate(s) to existing subscribers; "
            f"linked {linked} mirrored transactions."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)
