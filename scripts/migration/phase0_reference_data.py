"""Phase 0: Migrate reference/foundation data from Splynx.

Migrates (in order):
1. locations → PopSite
2. partners → Reseller
3. tax rates → TaxRate
4. payment_types → PaymentChannel + CollectionAccount
5. tariffs_internet → CatalogOffer + OfferPrice
6. tariffs_custom → CatalogOffer + OfferPrice
7. tariff junction tables → OfferResellerAvailability, OfferLocationAvailability, etc.
8. routers → NasDevice
9. IP pools → IpPool + IpBlock
10. billing_transactions_categories → LedgerCategory mapping (logged, not a table)

All records get SplynxIdMapping entries for bidirectional lookup.
"""

from __future__ import annotations

import logging
import sys
import uuid
from decimal import Decimal

from sqlalchemy import select

from scripts.migration.db_connections import (
    dotmac_session,
    fetch_all,
    splynx_connection,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def migrate_locations(conn, db) -> dict[int, uuid.UUID]:
    """Migrate Splynx locations → PopSite."""
    from app.models.network_monitoring import PopSite
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    rows = fetch_all(conn, "SELECT * FROM locations WHERE deleted='0' ORDER BY id")
    mapping: dict[int, uuid.UUID] = {}
    created = 0

    for row in rows:
        existing = db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.location,
                SplynxIdMapping.splynx_id == row["id"],
            )
        ).first()
        if existing:
            mapping[row["id"]] = existing.dotmac_id
            continue

        pop_site = PopSite(
            name=row["name"],
            is_active=True,
        )
        db.add(pop_site)
        db.flush()

        db.add(SplynxIdMapping(
            entity_type=SplynxEntityType.location,
            splynx_id=row["id"],
            dotmac_id=pop_site.id,
            metadata_={"splynx_name": row["name"]},
        ))
        mapping[row["id"]] = pop_site.id
        created += 1

    db.flush()
    logger.info("Locations: %d created, %d total", created, len(mapping))
    return mapping


def migrate_partners(conn, db) -> dict[int, uuid.UUID]:
    """Migrate Splynx partners → Reseller."""
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping
    from app.models.subscriber import Reseller

    # Include deleted partners so customers retain their reseller FK
    rows = fetch_all(conn, "SELECT * FROM partners ORDER BY id")
    mapping: dict[int, uuid.UUID] = {}
    created = 0

    for row in rows:
        existing = db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.partner,
                SplynxIdMapping.splynx_id == row["id"],
            )
        ).first()
        if existing:
            mapping[row["id"]] = existing.dotmac_id
            continue

        is_deleted = row.get("deleted") == "1"
        reseller = Reseller(
            name=row["name"],
            code=f"SPL-{row['id']}",
            contact_email=(row.get("email") or "")[:255] or None,
            contact_phone=(row.get("phone") or "")[:40] or None,
            is_active=not is_deleted,
        )
        db.add(reseller)
        db.flush()

        db.add(SplynxIdMapping(
            entity_type=SplynxEntityType.partner,
            splynx_id=row["id"],
            dotmac_id=reseller.id,
            metadata_={"splynx_name": row["name"], "deleted": is_deleted},
        ))
        mapping[row["id"]] = reseller.id
        created += 1

    db.flush()
    logger.info("Partners/Resellers: %d created, %d total", created, len(mapping))
    return mapping


def migrate_tax_rates(conn, db) -> dict[int, uuid.UUID]:
    """Migrate Splynx tax → TaxRate."""
    from app.models.billing import TaxRate
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    rows = fetch_all(conn, "SELECT * FROM tax")
    mapping: dict[int, uuid.UUID] = {}
    created = 0

    for row in rows:
        existing = db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.tariff,
                SplynxIdMapping.splynx_id == -row["id"],  # Negative to avoid tariff collision
            )
        ).first()
        if existing:
            mapping[row["id"]] = existing.dotmac_id
            continue

        is_archived = row.get("archived") in (1, "1", True)
        tax_rate = TaxRate(
            name=row["name"],
            code=f"tax-{row['id']}",
            rate=Decimal(str(row["rate"])),
            is_active=not is_archived,
        )
        db.add(tax_rate)
        db.flush()

        # Use a separate namespace to avoid collision with tariff IDs
        db.add(SplynxIdMapping(
            entity_type=SplynxEntityType.tariff,
            splynx_id=-row["id"],  # Negative = tax rate
            dotmac_id=tax_rate.id,
            metadata_={"type": "tax_rate", "splynx_name": row["name"], "rate": str(row["rate"])},
        ))
        mapping[row["id"]] = tax_rate.id
        created += 1

    db.flush()
    logger.info("Tax rates: %d created, %d total", created, len(mapping))
    return mapping


def migrate_tariffs(conn, db, tax_mapping: dict[int, uuid.UUID]) -> dict[int, uuid.UUID]:
    """Migrate Splynx tariffs_internet → CatalogOffer + OfferPrice."""
    from app.models.catalog import (
        AccessType,
        BillingCycle,
        CatalogOffer,
        OfferPrice,
        OfferStatus,
        PriceBasis,
        PriceType,
        ServiceType,
    )
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    rows = fetch_all(conn, "SELECT * FROM tariffs_internet ORDER BY id")
    mapping: dict[int, uuid.UUID] = {}
    created = 0

    for row in rows:
        existing = db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.tariff,
                SplynxIdMapping.splynx_id == row["id"],
            )
        ).first()
        if existing:
            mapping[row["id"]] = existing.dotmac_id
            continue

        is_deleted = row.get("deleted") == "1"
        offer = CatalogOffer(
            name=row["title"],
            code=(row.get("service_name") or f"inet-{row['id']}")[:60],
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            billing_cycle=BillingCycle.monthly,
            speed_download_mbps=(row.get("speed_download") or 0) // 1000 or None,
            speed_upload_mbps=(row.get("speed_upload") or 0) // 1000 or None,
            aggregation=row.get("aggregation"),
            priority=row.get("priority"),
            available_for_services=row.get("available_for_services") == "1",
            show_on_customer_portal=row.get("show_on_customer_portal") == "1",
            with_vat=row.get("with_vat") == "1",
            vat_percent=Decimal(str(row.get("vat_percent") or "0")),
            splynx_tariff_id=row["id"],
            splynx_service_name=row.get("service_name"),
            splynx_tax_id=row.get("tax_id"),
            status=OfferStatus.archived if is_deleted else OfferStatus.active,
            is_active=not is_deleted,
        )
        db.add(offer)
        db.flush()

        # Create default price
        price_amount = Decimal(str(row.get("price") or "0"))
        offer_price = OfferPrice(
            offer_id=offer.id,
            price_type=PriceType.recurring,
            currency="NGN",
            amount=price_amount,
            billing_cycle=BillingCycle.monthly,
            is_active=not is_deleted,
        )
        db.add(offer_price)

        db.add(SplynxIdMapping(
            entity_type=SplynxEntityType.tariff,
            splynx_id=row["id"],
            dotmac_id=offer.id,
            metadata_={
                "title": row["title"],
                "price": str(price_amount),
                "speed_down_kbps": row.get("speed_download"),
                "speed_up_kbps": row.get("speed_upload"),
                "deleted": is_deleted,
            },
        ))
        mapping[row["id"]] = offer.id
        created += 1

    db.flush()
    logger.info("Tariffs (internet): %d created, %d total", created, len(mapping))
    return mapping


def migrate_custom_tariffs(conn, db, tax_mapping: dict[int, uuid.UUID]) -> dict[int, uuid.UUID]:
    """Migrate Splynx tariffs_custom → CatalogOffer."""
    from app.models.catalog import (
        AccessType,
        BillingCycle,
        CatalogOffer,
        OfferPrice,
        OfferStatus,
        PriceBasis,
        PriceType,
        ServiceType,
    )
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    rows = fetch_all(conn, "SELECT * FROM tariffs_custom ORDER BY id")
    mapping: dict[int, uuid.UUID] = {}
    created = 0

    for row in rows:
        # Use offset to avoid ID collision with internet tariffs
        mapping_id = 100000 + row["id"]
        existing = db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.tariff,
                SplynxIdMapping.splynx_id == mapping_id,
            )
        ).first()
        if existing:
            mapping[row["id"]] = existing.dotmac_id
            continue

        is_deleted = row.get("deleted") == "1"
        offer = CatalogOffer(
            name=row["title"],
            code=f"custom-{row['id']}",
            service_type=ServiceType.business,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            billing_cycle=BillingCycle.monthly,
            splynx_tariff_id=mapping_id,
            status=OfferStatus.archived if is_deleted else OfferStatus.active,
            is_active=not is_deleted,
        )
        db.add(offer)
        db.flush()

        price_amount = Decimal(str(row.get("price") or "0"))
        db.add(OfferPrice(
            offer_id=offer.id,
            price_type=PriceType.recurring,
            currency="NGN",
            amount=price_amount,
            billing_cycle=BillingCycle.monthly,
            is_active=not is_deleted,
        ))

        db.add(SplynxIdMapping(
            entity_type=SplynxEntityType.tariff,
            splynx_id=mapping_id,
            dotmac_id=offer.id,
            metadata_={"title": row["title"], "type": "custom", "price": str(price_amount)},
        ))
        mapping[row["id"]] = offer.id
        created += 1

    db.flush()
    logger.info("Tariffs (custom): %d created, %d total", created, len(mapping))
    return mapping


def migrate_tariff_availability(
    conn, db,
    tariff_mapping: dict[int, uuid.UUID],
    partner_mapping: dict[int, uuid.UUID],
    location_mapping: dict[int, uuid.UUID],
) -> None:
    """Migrate tariff junction tables → Offer availability."""
    from app.models.catalog import BillingMode
    from app.models.offer_availability import (
        OfferBillingModeAvailability,
        OfferCategoryAvailability,
        OfferLocationAvailability,
        OfferResellerAvailability,
    )
    from app.models.subscriber import SubscriberCategory

    # tariffs_internet_to_partners → OfferResellerAvailability
    rows = fetch_all(conn, "SELECT * FROM tariffs_internet_to_partners")
    reseller_count = 0
    for row in rows:
        offer_id = tariff_mapping.get(row["tariff_id"])
        reseller_id = partner_mapping.get(row["partner_id"])
        if offer_id and reseller_id:
            db.add(OfferResellerAvailability(
                offer_id=offer_id, reseller_id=reseller_id,
            ))
            reseller_count += 1
    logger.info("Offer-reseller availability: %d", reseller_count)

    # tariffs_internet_to_locations → OfferLocationAvailability
    rows = fetch_all(conn, "SELECT * FROM tariffs_internet_to_locations")
    location_count = 0
    for row in rows:
        offer_id = tariff_mapping.get(row["tariff_id"])
        pop_site_id = location_mapping.get(row["location_id"])
        if offer_id and pop_site_id:
            db.add(OfferLocationAvailability(
                offer_id=offer_id, pop_site_id=pop_site_id,
            ))
            location_count += 1
    logger.info("Offer-location availability: %d", location_count)

    # tariffs_internet_to_customer_categories → OfferCategoryAvailability
    category_map = {
        "person": SubscriberCategory.residential,
        "company": SubscriberCategory.business,
    }
    rows = fetch_all(conn, "SELECT * FROM tariffs_internet_to_customer_categories")
    cat_count = 0
    for row in rows:
        offer_id = tariff_mapping.get(row["tariff_id"])
        cat = category_map.get(row.get("customer_category"))
        if offer_id and cat:
            db.add(OfferCategoryAvailability(
                offer_id=offer_id, subscriber_category=cat,
            ))
            cat_count += 1
    logger.info("Offer-category availability: %d", cat_count)

    # tariffs_internet_to_billing_types → OfferBillingModeAvailability
    billing_map = {
        "recurring": BillingMode.postpaid,
        "prepaid": BillingMode.prepaid,
        "prepaid_monthly": BillingMode.prepaid,
    }
    rows = fetch_all(conn, "SELECT * FROM tariffs_internet_to_billing_types")
    billing_count = 0
    seen_billing: set[tuple] = set()
    for row in rows:
        offer_id = tariff_mapping.get(row["tariff_id"])
        mode = billing_map.get(row.get("billing_type"))
        if offer_id and mode:
            key = (offer_id, mode.value)
            if key in seen_billing:
                continue
            seen_billing.add(key)
            db.add(OfferBillingModeAvailability(
                offer_id=offer_id, billing_mode=mode,
            ))
            billing_count += 1
    logger.info("Offer-billing mode availability: %d", billing_count)

    db.flush()


def migrate_routers(
    conn, db,
    location_mapping: dict[int, uuid.UUID],
) -> dict[int, uuid.UUID]:
    """Migrate Splynx routers → NasDevice."""
    from app.models.catalog import NasDevice, NasDeviceStatus
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    rows = fetch_all(conn, "SELECT * FROM routers ORDER BY id")
    mapping: dict[int, uuid.UUID] = {}
    created = 0

    for row in rows:
        existing = db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.router,
                SplynxIdMapping.splynx_id == row["id"],
            )
        ).first()
        if existing:
            mapping[row["id"]] = existing.dotmac_id
            continue

        is_deleted = row.get("deleted") == "1"
        pop_site_id = location_mapping.get(row.get("location_id"))

        nas = NasDevice(
            name=row["title"],
            ip_address=row.get("ip"),
            nas_ip=row.get("nas_ip"),
            pop_site_id=pop_site_id,
            shared_secret=row.get("radius_secret"),
            status=NasDeviceStatus.decommissioned if is_deleted else NasDeviceStatus.active,
            is_active=not is_deleted,
        )
        db.add(nas)
        db.flush()

        db.add(SplynxIdMapping(
            entity_type=SplynxEntityType.router,
            splynx_id=row["id"],
            dotmac_id=nas.id,
            metadata_={
                "title": row["title"],
                "ip": row.get("ip"),
                "nas_ip": row.get("nas_ip"),
                "auth_method": row.get("authorization_method"),
                "acct_method": row.get("accounting_method"),
                "radius_secret": row.get("radius_secret"),
                "deleted": is_deleted,
            },
        ))
        mapping[row["id"]] = nas.id
        created += 1

    db.flush()
    logger.info("Routers/NAS: %d created, %d total", created, len(mapping))
    return mapping


def migrate_ip_pools(conn, db) -> None:
    """Migrate Splynx ipv4_networks → IpPool (one per network)."""
    from app.models.network import IpPool, IPVersion

    rows = fetch_all(conn, "SELECT * FROM ipv4_networks WHERE deleted='0' ORDER BY id")
    created = 0

    for row in rows:
        network = row.get("network", "")
        if not network:
            continue

        # Determine CIDR — Splynx stores network without mask
        # Most are /24, use network column as-is with /24 default
        cidr = network if "/" in network else f"{network}/24"
        name = f"Pool-{row['id']}-{network}"

        pool = IpPool(
            name=name[:120],
            ip_version=IPVersion.ipv4,
            cidr=cidr,
            is_active=True,
            notes=f"Migrated from Splynx ipv4_networks.id={row['id']}",
        )
        db.add(pool)
        created += 1

    db.flush()
    logger.info("IP pools: %d created", created)


def run_phase0(dry_run: bool = True) -> None:
    """Execute Phase 0 migration."""
    logger.info("=== Phase 0: Reference Data Migration ===")

    with splynx_connection() as conn:
        with dotmac_session() as db:
            if dry_run:
                logger.info("DRY RUN — counting source data only")
                tables = [
                    ("locations", "SELECT COUNT(*) as cnt FROM locations WHERE deleted='0'"),
                    ("partners (all)", "SELECT COUNT(*) as cnt FROM partners"),
                    ("partners (deleted)", "SELECT COUNT(*) as cnt FROM partners WHERE deleted='1'"),
                    ("tax", "SELECT COUNT(*) as cnt FROM tax"),
                    ("tariffs_internet", "SELECT COUNT(*) as cnt FROM tariffs_internet"),
                    ("tariffs_custom", "SELECT COUNT(*) as cnt FROM tariffs_custom"),
                    ("routers", "SELECT COUNT(*) as cnt FROM routers"),
                    ("ipv4_networks", "SELECT COUNT(*) as cnt FROM ipv4_networks WHERE deleted='0'"),
                    ("tariffs_to_partners", "SELECT COUNT(*) as cnt FROM tariffs_internet_to_partners"),
                    ("tariffs_to_locations", "SELECT COUNT(*) as cnt FROM tariffs_internet_to_locations"),
                ]
                for name, query in tables:
                    rows = fetch_all(conn, query)
                    logger.info("  %s: %d rows", name, rows[0]["cnt"])
                logger.info("Run with --execute to migrate")
                return

            # Execute migration in order
            location_mapping = migrate_locations(conn, db)
            partner_mapping = migrate_partners(conn, db)
            tax_mapping = migrate_tax_rates(conn, db)
            tariff_mapping = migrate_tariffs(conn, db, tax_mapping)
            custom_tariff_mapping = migrate_custom_tariffs(conn, db, tax_mapping)
            migrate_tariff_availability(
                conn, db, tariff_mapping, partner_mapping, location_mapping,
            )
            router_mapping = migrate_routers(conn, db, location_mapping)
            migrate_ip_pools(conn, db)

            db.commit()
            logger.info("=== Phase 0 complete — committed ===")

            # Summary
            from sqlalchemy import func

            from app.models.splynx_mapping import SplynxIdMapping
            counts = db.execute(
                select(SplynxIdMapping.entity_type, func.count(SplynxIdMapping.id))
                .group_by(SplynxIdMapping.entity_type)
            ).all()
            logger.info("--- SplynxIdMapping summary ---")
            for entity_type, count in counts:
                logger.info("  %s: %d", entity_type.value, count)


if __name__ == "__main__":
    if "--execute" in sys.argv:
        run_phase0(dry_run=False)
    else:
        run_phase0(dry_run=True)
        print("\nTo execute: poetry run python scripts/migration/phase0_reference_data.py --execute")
