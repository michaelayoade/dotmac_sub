"""Phase 1: Migrate customers and services from Splynx.

Migrates (in order):
1. customers (non-lead) → Subscriber + SplynxIdMapping
2. customer_billing → inline billing fields on Subscriber
3. customer custom field values → SubscriberCustomField
4. services_internet → Subscription + AccessCredential
5. services_custom → Subscription

Handles:
- Email deduplication (1,105 duplicates, mostly reseller accounts)
- Name splitting (Splynx stores full name, DotMac Sub has first/last)
- Status mapping (Splynx new/active/blocked/disabled → DotMac active/suspended/canceled)
- Passwords migrated as-is (base64-encoded, stored via credential_crypto)
- Deleted records included (is_active=False)
"""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from scripts.migration.db_connections import (
    dotmac_session,
    fetch_all,
    fetch_batched,
    splynx_connection,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Status mappings ---
CUSTOMER_STATUS_MAP = {
    "new": "new",
    "active": "active",
    "blocked": "suspended",
    "disabled": "disabled",
}

CATEGORY_MAP = {
    "person": "residential",
    "company": "business",
}

BILLING_TYPE_MAP = {
    "recurring": "postpaid",
    "prepaid": "prepaid",
    "prepaid_monthly": "prepaid",
}

SERVICE_STATUS_MAP = {
    "active": "active",
    "blocked": "suspended",
    "disabled": "disabled",
    "new": "pending",
    "stopped": "stopped",
    "hidden": "archived",
}


def _split_name(full_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last). Handles edge cases."""
    parts = (full_name or "").strip().split(None, 1)
    if len(parts) == 2:
        return parts[0][:80], parts[1][:80]
    if len(parts) == 1:
        return parts[0][:80], parts[0][:80]
    return "Unknown", "Unknown"


def _dedup_email(email: str, customer_id: int, seen_emails: set[str]) -> str:
    """Make email unique by appending +splynx_id if duplicate."""
    if not email or not email.strip():
        return f"no-email+{customer_id}@splynx.local"

    email = email.strip().lower()
    # Handle multi-email fields (take first)
    if "," in email:
        email = email.split(",")[0].strip()
    if " " in email:
        email = email.split()[0].strip()

    if email not in seen_emails:
        seen_emails.add(email)
        return email

    # Deduplicate with +id suffix
    local, _, domain = email.rpartition("@")
    if not domain:
        domain = "splynx.local"
        local = email
    deduped = f"{local}+{customer_id}@{domain}"
    seen_emails.add(deduped)
    return deduped


def _parse_date(val) -> datetime | None:
    """Parse Splynx date/datetime values."""
    if not val:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=UTC) if val.tzinfo is None else val
    try:
        from datetime import date as date_type
        if isinstance(val, date_type):
            return datetime(val.year, val.month, val.day, tzinfo=UTC)
    except (ValueError, TypeError):
        pass
    return None


def _map_customer_status(status_raw: str | None, *, is_deleted: bool):
    from app.models.subscriber import SubscriberStatus

    if is_deleted:
        return SubscriberStatus.canceled
    status_str = CUSTOMER_STATUS_MAP.get((status_raw or "new").strip().lower(), "active")
    return {
        "new": SubscriberStatus.new,
        "active": SubscriberStatus.active,
        "suspended": SubscriberStatus.suspended,
        "disabled": SubscriberStatus.disabled,
    }.get(status_str, SubscriberStatus.active)


def _map_service_status(status_raw: str | None, *, is_deleted: bool):
    from app.models.catalog import SubscriptionStatus

    if is_deleted:
        return SubscriptionStatus.canceled
    status_str = SERVICE_STATUS_MAP.get((status_raw or "active").strip().lower(), "active")
    return {
        "active": SubscriptionStatus.active,
        "suspended": SubscriptionStatus.suspended,
        "disabled": SubscriptionStatus.disabled,
        "pending": SubscriptionStatus.pending,
        "stopped": SubscriptionStatus.stopped,
        "archived": SubscriptionStatus.archived,
    }.get(status_str, SubscriptionStatus.active)


def _map_billing_mode(billing_type_raw: str | None):
    from app.models.catalog import BillingMode

    mapped = BILLING_TYPE_MAP.get((billing_type_raw or "").strip().lower(), "prepaid")
    return BillingMode.postpaid if mapped == "postpaid" else BillingMode.prepaid


def migrate_customers(conn, db) -> dict[int, uuid.UUID]:
    """Migrate Splynx customers → Subscriber."""
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping
    from app.models.subscriber import Subscriber, SubscriberStatus, UserType

    # Load existing mappings (partner, tax)
    partner_mappings = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.partner
            )
        ).all()
    }

    mapping: dict[int, uuid.UUID] = {}
    seen_emails: set[str] = set()
    seen_sub_numbers: set[str] = set()
    created = 0
    skipped = 0

    # Load existing Splynx mappings to skip already-migrated
    existing_maps = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.customer
            )
        ).all()
    }
    if existing_maps:
        # Also load their emails/subscriber_numbers into seen sets
        for dotmac_id in existing_maps.values():
            sub = db.get(Subscriber, dotmac_id)
            if sub:
                seen_emails.add(sub.email.lower())
                if sub.subscriber_number:
                    seen_sub_numbers.add(sub.subscriber_number)
        mapping.update(existing_maps)
        logger.info("Found %d existing customer mappings, skipping", len(existing_maps))

    query = """
        SELECT c.*, cb.billing_date, cb.billing_due, cb.grace_period,
               cb.blocking_period, cb.deposit as cb_deposit, cb.payment_method as cb_payment_method,
               cb.partner_percent, cb.enabled as billing_enabled
        FROM customers c
        LEFT JOIN customer_billing cb ON cb.customer_id = c.id
        WHERE c.category != 'lead'
        ORDER BY c.id
    """

    for batch in fetch_batched(conn, query, batch_size=500):
        for row in batch:
            cid = row["id"]
            if cid in existing_maps:
                continue

            is_deleted = row.get("deleted") == "1"
            first_name, last_name = _split_name(row["name"])
            email = _dedup_email(row.get("email", ""), cid, seen_emails)
            status_raw = row.get("status", "new")
            status_enum = _map_customer_status(status_raw, is_deleted=is_deleted)

            # Reseller mapping
            reseller_id = partner_mappings.get(row.get("partner_id"))

            # Category → metadata
            category = CATEGORY_MAP.get(row.get("category", "person"), "residential")

            # Deduplicate subscriber_number (Splynx login)
            raw_login = str(row.get("login") or f"SPL-{cid}").strip()[:80]
            sub_number = raw_login
            if sub_number in seen_sub_numbers:
                sub_number = f"{raw_login}-{cid}"
            seen_sub_numbers.add(sub_number)

            subscriber = Subscriber(
                first_name=first_name,
                last_name=last_name,
                display_name=(row["name"] or "")[:120],
                email=email,
                phone=(row.get("phone") or "")[:40] or None,
                address_line1=(row.get("street_1") or "")[:120] or None,
                city=(row.get("city") or "")[:80] or None,
                postal_code=(row.get("zip_code") or "")[:20] or None,
                country_code="NG",
                subscriber_number=sub_number,
                account_number=str(cid),
                account_start_date=_parse_date(row.get("date_add")),
                status=status_enum,
                user_type=UserType.customer,
                is_active=not is_deleted,
                reseller_id=reseller_id,
                # Billing fields from customer_billing
                billing_enabled=row.get("billing_enabled") in (1, "1", True),
                billing_day=row.get("billing_date"),
                payment_due_days=row.get("billing_due"),
                grace_period_days=row.get("grace_period"),
                deposit=Decimal(str(row.get("cb_deposit") or "0")),
                mrr_total=Decimal(str(row.get("mrr_total") or "0")),
                splynx_customer_id=cid,
                metadata_={
                    "subscriber_category": category,
                    "splynx_deleted": is_deleted,
                    "splynx_status": status_raw,
                    "splynx_date_add": (
                        _parse_date(row.get("date_add")).isoformat()
                        if _parse_date(row.get("date_add")) is not None
                        else None
                    ),
                    "splynx_last_update": (
                        _parse_date(row.get("last_update")).isoformat()
                        if _parse_date(row.get("last_update")) is not None
                        else None
                    ),
                    "splynx_category": row.get("category"),
                    "splynx_billing_type": row.get("billing_type"),
                    "splynx_login": row.get("login"),
                    "splynx_partner_percent": str(row.get("partner_percent") or ""),
                },
            )
            db.add(subscriber)

            try:
                db.flush()
            except Exception as e:
                db.rollback()
                logger.warning("Failed to create subscriber for customer %d: %s", cid, e)
                skipped += 1
                continue

            db.add(SplynxIdMapping(
                entity_type=SplynxEntityType.customer,
                splynx_id=cid,
                dotmac_id=subscriber.id,
            ))
            mapping[cid] = subscriber.id
            created += 1

        db.flush()
        logger.info("Customers batch: %d created so far (%d skipped)", created, skipped)

    db.flush()
    logger.info("Customers: %d created, %d skipped, %d total", created, skipped, len(mapping))
    return mapping


def migrate_custom_fields(conn, db, customer_mapping: dict[int, uuid.UUID]) -> None:
    """Migrate customer custom field values → SubscriberCustomField."""
    from app.models.subscriber import SubscriberCustomField

    existing_pairs = {
        (str(cf.subscriber_id), cf.key)
        for cf in db.scalars(select(SubscriberCustomField)).all()
    }

    # Get field definitions
    fields = fetch_all(conn, "SELECT * FROM customers_fields WHERE deleted='0'")
    field_defs = {f["name"]: f for f in fields}

    query = "SELECT * FROM customers_values ORDER BY id"
    created = 0
    skipped = 0

    for batch in fetch_batched(conn, query, batch_size=2000):
        for row in batch:
            subscriber_id = customer_mapping.get(row.get("id"))
            if not subscriber_id:
                skipped += 1
                continue

            field_name = row.get("name", "")
            value = row.get("value", "")
            if not field_name or not value:
                skipped += 1
                continue

            # Skip deleted/addon fields
            field_def = field_defs.get(field_name)
            if field_def and field_def.get("type") == "add-on":
                skipped += 1
                continue

            key = (str(subscriber_id), field_name[:80])
            if key in existing_pairs:
                skipped += 1
                continue

            cf = SubscriberCustomField(
                subscriber_id=subscriber_id,
                key=field_name[:80],
                value_text=str(value)[:2000] if value else None,
            )
            db.add(cf)
            existing_pairs.add(key)
            created += 1

        db.flush()

    logger.info("Custom fields: %d created, %d skipped", created, skipped)


def migrate_services(
    conn, db,
    customer_mapping: dict[int, uuid.UUID],
) -> dict[int, uuid.UUID]:
    """Migrate Splynx services_internet → Subscription + AccessCredential."""
    from app.models.catalog import (
        AccessCredential,
        BillingMode,
        ContractTerm,
        Subscription,
        SubscriptionStatus,
    )
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    # Load tariff and router mappings
    tariff_mappings = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.tariff,
                SplynxIdMapping.splynx_id > 0,  # Positive = tariffs, negative = tax
            )
        ).all()
    }
    router_mappings = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.router
            )
        ).all()
    }

    # Load existing service mappings
    existing_maps = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.service
            )
        ).all()
    }

    # Track usernames for dedup
    existing_usernames = set(
        db.scalars(select(AccessCredential.username)).all()
    )

    mapping: dict[int, uuid.UUID] = dict(existing_maps)
    created = 0
    skipped = 0
    creds_created = 0

    query = "SELECT * FROM services_internet ORDER BY id"

    for batch in fetch_batched(conn, query, batch_size=500):
        for row in batch:
            sid = row["id"]
            if sid in existing_maps:
                continue

            subscriber_id = customer_mapping.get(row.get("customer_id"))
            if not subscriber_id:
                skipped += 1
                continue

            offer_id = tariff_mappings.get(row.get("tariff_id"))
            if not offer_id:
                skipped += 1
                continue

            is_deleted = row.get("deleted") == "1"
            status_raw = row.get("status", "active")
            status_enum = _map_service_status(status_raw, is_deleted=is_deleted)

            nas_device_id = router_mappings.get(row.get("router_id"))

            # Parse dates — handle '0000-00-00'
            start_date = row.get("start_date")
            end_date = row.get("end_date")
            start_at = _parse_date(start_date) if start_date and str(start_date) != "0000-00-00" else None
            end_at = _parse_date(end_date) if end_date and str(end_date) != "0000-00-00" else None

            # Discount fields
            has_discount = row.get("discount") in ("1", 1, True)
            discount_start = row.get("discount_start_date")
            discount_end = row.get("discount_end_date")
            # Map Splynx discount_type to DotMac enum
            raw_disc_type = row.get("discount_type")
            disc_type_map = {"percent": "percentage", "fixed": "fixed"}
            mapped_disc_type = disc_type_map.get(raw_disc_type, raw_disc_type) if raw_disc_type else None

            billing_mode = _map_billing_mode(row.get("billing_type"))

            subscription = Subscription(
                subscriber_id=subscriber_id,
                offer_id=offer_id,
                provisioning_nas_device_id=nas_device_id,
                status=status_enum,
                billing_mode=billing_mode,
                contract_term=ContractTerm.month_to_month,
                start_at=start_at,
                end_at=end_at,
                splynx_service_id=sid,
                router_id=row.get("router_id"),
                service_description=(row.get("description") or "")[:500] or None,
                quantity=row.get("quantity") or 1,
                unit=(row.get("unit") or "")[:40] or None,
                unit_price=Decimal(str(row.get("unit_price") or "0")),
                login=(row.get("login") or "")[:120] or None,
                ipv4_address=(row.get("ipv4") or "")[:64] or None,
                ipv6_address=(row.get("ipv6") or "")[:128] or None,
                mac_address=(row.get("mac") or "")[:64] or None,
                discount=has_discount,
                discount_value=Decimal(str(row.get("discount_value") or "0")) if has_discount else None,
                discount_type=mapped_disc_type if has_discount else None,
                discount_start_at=_parse_date(discount_start) if discount_start and str(discount_start) != "0000-00-00" else None,
                discount_end_at=_parse_date(discount_end) if discount_end and str(discount_end) != "0000-00-00" else None,
                discount_description=(row.get("discount_text") or "")[:512] or None,
                service_status_raw=status_raw,
            )
            db.add(subscription)

            try:
                db.flush()
            except Exception as e:
                db.rollback()
                logger.warning("Failed subscription for service %d: %s", sid, e)
                skipped += 1
                continue

            # Create AccessCredential for RADIUS auth
            login = (row.get("login") or "").strip()
            password = row.get("password") or ""
            if login and login not in existing_usernames:
                from app.services.credential_crypto import encrypt_credential

                cred = AccessCredential(
                    subscriber_id=subscriber_id,
                    username=login[:120],
                    secret_hash=encrypt_credential(password[:255]) if password else None,
                    is_active=not is_deleted and status_enum == SubscriptionStatus.active,
                )
                db.add(cred)
                existing_usernames.add(login)
                creds_created += 1

            db.add(SplynxIdMapping(
                entity_type=SplynxEntityType.service,
                splynx_id=sid,
                dotmac_id=subscription.id,
            ))
            mapping[sid] = subscription.id
            created += 1

        db.flush()
        logger.info(
            "Services batch: %d created, %d creds (%d skipped)",
            created, creds_created, skipped,
        )

    db.flush()
    logger.info(
        "Services: %d created, %d access credentials, %d skipped, %d total",
        created, creds_created, skipped, len(mapping),
    )
    return mapping


def migrate_custom_services(
    conn, db,
    customer_mapping: dict[int, uuid.UUID],
) -> None:
    """Migrate Splynx services_custom → Subscription."""
    from app.models.catalog import (
        BillingMode,
        ContractTerm,
        Subscription,
        SubscriptionStatus,
    )
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    # Custom tariff mappings use offset 100000+
    tariff_mappings = {
        m.splynx_id - 100000: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.tariff,
                SplynxIdMapping.splynx_id >= 100000,
            )
        ).all()
    }

    query = "SELECT * FROM services_custom ORDER BY id"
    rows = fetch_all(conn, query)
    existing_maps = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.service,
                SplynxIdMapping.splynx_id >= 200000,
            )
        ).all()
    }
    created = 0
    skipped = 0

    for row in rows:
        sid = row["id"]
        mapping_id = 200000 + sid
        if mapping_id in existing_maps:
            continue
        subscriber_id = customer_mapping.get(row.get("customer_id"))
        if not subscriber_id:
            skipped += 1
            continue

        offer_id = tariff_mappings.get(row.get("tariff_id"))
        if not offer_id:
            skipped += 1
            continue

        is_deleted = row.get("deleted") == "1"
        status_raw = row.get("status", "active")
        status_enum = _map_service_status(status_raw, is_deleted=is_deleted)

        start_date = row.get("start_date")
        start_at = _parse_date(start_date) if start_date and str(start_date) != "0000-00-00" else None

        subscription = Subscription(
            subscriber_id=subscriber_id,
            offer_id=offer_id,
            status=status_enum,
            billing_mode=_map_billing_mode(row.get("billing_type")),
            contract_term=ContractTerm.month_to_month,
            start_at=start_at,
            splynx_service_id=mapping_id,
            service_description=(row.get("description") or "")[:500] or None,
            quantity=row.get("quantity") or 1,
            unit_price=Decimal(str(row.get("unit_price") or "0")),
            service_status_raw=status_raw,
        )
        db.add(subscription)
        db.flush()
        db.add(SplynxIdMapping(
            entity_type=SplynxEntityType.service,
            splynx_id=mapping_id,
            dotmac_id=subscription.id,
        ))
        created += 1

    db.flush()
    logger.info("Custom services: %d created, %d skipped", created, skipped)


def run_phase1(dry_run: bool = True) -> None:
    """Execute Phase 1 migration."""
    logger.info("=== Phase 1: Customer & Service Migration ===")

    with splynx_connection() as conn:
        with dotmac_session() as db:
            if dry_run:
                logger.info("DRY RUN — counting source data only")
                tables = [
                    ("customers (non-lead)", "SELECT COUNT(*) as cnt FROM customers WHERE category != 'lead'"),
                    ("customers (non-lead, active)", "SELECT COUNT(*) as cnt FROM customers WHERE category != 'lead' AND status='active' AND deleted='0'"),
                    ("customers (non-lead, blocked)", "SELECT COUNT(*) as cnt FROM customers WHERE category != 'lead' AND status='blocked' AND deleted='0'"),
                    ("customers (non-lead, deleted)", "SELECT COUNT(*) as cnt FROM customers WHERE category != 'lead' AND deleted='1'"),
                    ("customer_billing", "SELECT COUNT(*) as cnt FROM customer_billing"),
                    ("customers_values", "SELECT COUNT(*) as cnt FROM customers_values"),
                    ("services_internet", "SELECT COUNT(*) as cnt FROM services_internet"),
                    ("services_custom", "SELECT COUNT(*) as cnt FROM services_custom"),
                ]
                for name, query in tables:
                    rows = fetch_all(conn, query)
                    logger.info("  %s: %d rows", name, rows[0]["cnt"])
                logger.info("Run with --execute to migrate")
                return

            # Step 1: Customers → Subscribers
            customer_mapping = migrate_customers(conn, db)
            db.commit()
            logger.info("--- Customers committed ---")

            # Step 2: Custom fields
            migrate_custom_fields(conn, db, customer_mapping)
            db.commit()
            logger.info("--- Custom fields committed ---")

            # Step 3: Internet services → Subscriptions + AccessCredentials
            service_mapping = migrate_services(conn, db, customer_mapping)
            db.commit()
            logger.info("--- Internet services committed ---")

            # Step 4: Custom services → Subscriptions
            migrate_custom_services(conn, db, customer_mapping)
            db.commit()
            logger.info("--- Custom services committed ---")

            # Summary
            from app.models.splynx_mapping import SplynxIdMapping
            counts = db.execute(
                select(SplynxIdMapping.entity_type, func.count(SplynxIdMapping.id))
                .group_by(SplynxIdMapping.entity_type)
            ).all()
            logger.info("=== Phase 1 complete ===")
            logger.info("--- SplynxIdMapping summary ---")
            for entity_type, count in counts:
                logger.info("  %s: %d", entity_type.value, count)


if __name__ == "__main__":
    if "--execute" in sys.argv:
        run_phase1(dry_run=False)
    else:
        run_phase1(dry_run=True)
        print("\nTo execute: poetry run python -m scripts.migration.phase1_customers_services --execute")
