"""Sync Splynx customer accounts and details into DotMac Sub.

This intentionally does not sync invoices, payments, or services.
"""

from __future__ import annotations

import argparse
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping
from app.models.subscriber import Subscriber, SubscriberCustomField, UserType
from scripts.migration.db_connections import (
    dotmac_session,
    fetch_all,
    fetch_batched,
    splynx_connection,
)
from scripts.migration.incremental_sync import _is_splynx_deleted
from scripts.migration.phase1_customers_services import (
    CATEGORY_MAP,
    _dedup_email,
    _map_customer_status,
    _parse_date,
    _split_name,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _decimal(value) -> Decimal:
    return Decimal(str(value or "0"))


def _int_or_none(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(value) -> str | None:
    parsed = _parse_date(value)
    return parsed.isoformat() if parsed else None


def _customer_metadata(row: dict, *, email_conflict: str | None = None) -> dict:
    category = CATEGORY_MAP.get(row.get("category", "person"), "residential")
    metadata = {
        "subscriber_category": category,
        "splynx_deleted": _is_splynx_deleted(row.get("deleted")),
        "splynx_status": row.get("status"),
        "splynx_date_add": _iso(row.get("date_add")),
        "splynx_last_update": _iso(row.get("last_update")),
        "splynx_category": row.get("category"),
        "splynx_billing_type": row.get("billing_type"),
        "splynx_login": row.get("login"),
        "splynx_partner_percent": str(row.get("partner_percent") or ""),
    }
    if email_conflict:
        metadata["splynx_email"] = email_conflict
        metadata["splynx_email_conflict"] = True
    return metadata


def _apply_customer_row(
    subscriber: Subscriber,
    row: dict,
    *,
    email_by_lower: dict[str, uuid.UUID],
    seen_emails: set[str],
    seen_subscriber_numbers: set[str],
    is_new: bool,
) -> bool:
    cid = int(row["id"])
    is_deleted = _is_splynx_deleted(row.get("deleted"))
    first_name, last_name = _split_name(row.get("name") or "")
    raw_email = str(row.get("email") or "").strip().lower()
    if is_new:
        email = _dedup_email(raw_email, cid, seen_emails)
    else:
        email = subscriber.email
        candidate = _dedup_email(raw_email, cid, set()) if raw_email else ""
        owner = email_by_lower.get(candidate.lower()) if candidate else None
        if candidate and (owner is None or owner == subscriber.id):
            email = candidate
            seen_emails.add(candidate.lower())

    email_conflict = None
    if raw_email and email.lower() != raw_email:
        owner = email_by_lower.get(raw_email)
        if owner is not None and owner != subscriber.id:
            email_conflict = raw_email

    status_enum = _map_customer_status(row.get("status", "new"), is_deleted=is_deleted)
    category = CATEGORY_MAP.get(row.get("category", "person"), "residential")
    raw_login = str(row.get("login") or f"SPL-{cid}").strip()[:80]
    subscriber_number = subscriber.subscriber_number or raw_login
    if (
        subscriber_number in seen_subscriber_numbers
        and subscriber.subscriber_number is None
    ):
        subscriber_number = f"{raw_login}-{cid}"[:80]
    seen_subscriber_numbers.add(subscriber_number)

    subscriber.first_name = first_name
    subscriber.last_name = last_name
    subscriber.display_name = (row.get("name") or "")[:120]
    subscriber.company_name = (
        subscriber.display_name if category == "business" else None
    )
    subscriber.email = email
    subscriber.phone = (row.get("phone") or "")[:40] or None
    subscriber.address_line1 = (row.get("street_1") or "")[:120] or None
    subscriber.city = (row.get("city") or "")[:80] or None
    subscriber.postal_code = (row.get("zip_code") or "")[:20] or None
    subscriber.country_code = "NG"
    subscriber.subscriber_number = subscriber_number
    subscriber.account_number = str(cid)
    subscriber.account_start_date = _parse_date(row.get("date_add"))
    subscriber.status = status_enum
    subscriber.user_type = UserType.customer
    subscriber.is_active = not is_deleted
    subscriber.billing_enabled = row.get("billing_enabled") in (1, "1", True)
    subscriber.billing_day = _int_or_none(row.get("billing_date"))
    subscriber.payment_due_days = _int_or_none(row.get("billing_due"))
    subscriber.grace_period_days = _int_or_none(row.get("grace_period"))
    subscriber.deposit = _decimal(row.get("cb_deposit"))
    subscriber.mrr_total = _decimal(row.get("mrr_total"))
    subscriber.splynx_customer_id = cid

    metadata = dict(subscriber.metadata_ or {})
    metadata.update(_customer_metadata(row, email_conflict=email_conflict))
    subscriber.metadata_ = metadata

    email_by_lower[email.lower()] = subscriber.id
    return is_new


def sync_customer_accounts(conn, db, *, dry_run: bool = True) -> dict[str, int]:
    existing_by_splynx = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.customer
            )
        ).all()
    }
    existing_by_column = {
        int(sub.splynx_customer_id): sub.id
        for sub in db.scalars(
            select(Subscriber).where(Subscriber.splynx_customer_id.is_not(None))
        ).all()
        if sub.splynx_customer_id is not None
    }
    email_by_lower = {
        email.lower(): subscriber_id
        for subscriber_id, email in db.execute(
            select(Subscriber.id, Subscriber.email)
        ).all()
        if email
    }
    seen_emails = set(email_by_lower)
    seen_subscriber_numbers = {
        value
        for (value,) in db.execute(
            select(Subscriber.subscriber_number).where(
                Subscriber.subscriber_number.is_not(None)
            )
        ).all()
        if value
    }

    query = """
        SELECT c.*, cb.billing_date, cb.billing_due, cb.grace_period,
               cb.blocking_period, cb.deposit as cb_deposit,
               cb.payment_method as cb_payment_method,
               cb.partner_percent, cb.enabled as billing_enabled
        FROM customers c
        LEFT JOIN customer_billing cb ON cb.customer_id = c.id
        WHERE c.category != 'lead'
        ORDER BY c.id
    """

    created = 0
    updated = 0
    mapped = 0
    skipped = 0

    for batch in fetch_batched(conn, query, batch_size=500):
        for row in batch:
            cid = int(row["id"])
            subscriber_id = existing_by_splynx.get(cid) or existing_by_column.get(cid)
            subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None
            is_new = subscriber is None
            if is_new:
                subscriber = Subscriber(
                    first_name="Unknown",
                    last_name="Unknown",
                    email=f"no-email+{cid}@splynx.local",
                )
                db.add(subscriber)
                db.flush()

            try:
                _apply_customer_row(
                    subscriber,
                    row,
                    email_by_lower=email_by_lower,
                    seen_emails=seen_emails,
                    seen_subscriber_numbers=seen_subscriber_numbers,
                    is_new=is_new,
                )
                if cid not in existing_by_splynx:
                    db.add(
                        SplynxIdMapping(
                            entity_type=SplynxEntityType.customer,
                            splynx_id=cid,
                            dotmac_id=subscriber.id,
                            metadata_={"source": "customer_accounts_details_sync"},
                        )
                    )
                    existing_by_splynx[cid] = subscriber.id
                    mapped += 1
                existing_by_column[cid] = subscriber.id
                created += 1 if is_new else 0
                updated += 0 if is_new else 1
            except Exception as exc:
                logger.warning("Customer %s skipped: %s", cid, exc)
                skipped += 1

        if not dry_run:
            db.flush()
        logger.info(
            "Customer accounts batch: %d created, %d updated, %d mapped, %d skipped",
            created,
            updated,
            mapped,
            skipped,
        )

    if dry_run:
        db.rollback()
    else:
        db.flush()

    return {
        "created": created,
        "updated": updated,
        "mapped": mapped,
        "skipped": skipped,
    }


def sync_customer_custom_fields(conn, db, *, dry_run: bool = True) -> dict[str, int]:
    valid_subscriber_ids = set(db.scalars(select(Subscriber.id)).all())
    customer_mapping = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.customer
            )
        ).all()
        if m.dotmac_id in valid_subscriber_ids
    }
    existing = {
        (cf.subscriber_id, cf.key): cf
        for cf in db.scalars(select(SubscriberCustomField)).all()
    }

    rows = fetch_all(conn, "SELECT * FROM customers_values ORDER BY id")
    created = 0
    updated = 0
    skipped = 0

    for row in rows:
        subscriber_id = customer_mapping.get(row.get("id"))
        field_name = str(row.get("name") or "").strip()[:120]
        value = str(row.get("value") or "")[:2000]
        if not subscriber_id or not field_name or not value:
            skipped += 1
            continue

        key = (subscriber_id, field_name)
        custom_field = existing.get(key)
        if custom_field is None:
            custom_field = SubscriberCustomField(
                subscriber_id=subscriber_id,
                key=field_name,
                value_text=value,
            )
            db.add(custom_field)
            existing[key] = custom_field
            created += 1
        elif custom_field.value_text != value or not custom_field.is_active:
            custom_field.value_text = value
            custom_field.is_active = True
            updated += 1
        else:
            skipped += 1

    if dry_run:
        db.rollback()
    else:
        db.flush()

    return {"created": created, "updated": updated, "skipped": skipped}


def run_customer_sync(
    *, dry_run: bool = True, custom_fields_only: bool = False
) -> dict[str, dict[str, int]]:
    started = datetime.now(UTC)
    logger.info(
        "=== Splynx customer accounts/details sync (%s) ===", started.isoformat()
    )
    with splynx_connection() as conn:
        with dotmac_session() as db:
            account_stats = {"created": 0, "updated": 0, "mapped": 0, "skipped": 0}
            if not custom_fields_only:
                account_stats = sync_customer_accounts(conn, db, dry_run=dry_run)
                if not dry_run:
                    db.commit()
            field_stats = sync_customer_custom_fields(conn, db, dry_run=dry_run)
            if not dry_run:
                db.commit()
    logger.info("Customer accounts: %s", account_stats)
    logger.info("Customer custom fields: %s", field_stats)
    return {"accounts": account_stats, "custom_fields": field_stats}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync only Splynx customer accounts and details."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write changes. Without this flag, the command rolls back.",
    )
    parser.add_argument(
        "--custom-fields-only",
        action="store_true",
        help="Only sync customer custom fields/details; skip account updates.",
    )
    args = parser.parse_args()
    run_customer_sync(
        dry_run=not args.execute,
        custom_fields_only=args.custom_fields_only,
    )
    if not args.execute:
        print("Dry run only. Re-run with --execute to write changes.")


if __name__ == "__main__":
    main()
