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

        db.add(
            SplynxIdMapping(
                entity_type=SplynxEntityType.location,
                splynx_id=row["id"],
                dotmac_id=pop_site.id,
                metadata_={"splynx_name": row["name"]},
            )
        )
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

        db.add(
            SplynxIdMapping(
                entity_type=SplynxEntityType.partner,
                splynx_id=row["id"],
                dotmac_id=reseller.id,
                metadata_={"splynx_name": row["name"], "deleted": is_deleted},
            )
        )
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
                SplynxIdMapping.splynx_id
                == -row["id"],  # Negative to avoid tariff collision
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
        db.add(
            SplynxIdMapping(
                entity_type=SplynxEntityType.tariff,
                splynx_id=-row["id"],  # Negative = tax rate
                dotmac_id=tax_rate.id,
                metadata_={
                    "type": "tax_rate",
                    "splynx_name": row["name"],
                    "rate": str(row["rate"]),
                },
            )
        )
        mapping[row["id"]] = tax_rate.id
        created += 1

    db.flush()
    logger.info("Tax rates: %d created, %d total", created, len(mapping))
    return mapping


def migrate_tariffs(
    conn, db, tax_mapping: dict[int, uuid.UUID]
) -> dict[int, uuid.UUID]:
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

        db.add(
            SplynxIdMapping(
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
            )
        )
        mapping[row["id"]] = offer.id
        created += 1

    db.flush()
    logger.info("Tariffs (internet): %d created, %d total", created, len(mapping))
    return mapping


def migrate_custom_tariffs(
    conn, db, tax_mapping: dict[int, uuid.UUID]
) -> dict[int, uuid.UUID]:
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
        db.add(
            OfferPrice(
                offer_id=offer.id,
                price_type=PriceType.recurring,
                currency="NGN",
                amount=price_amount,
                billing_cycle=BillingCycle.monthly,
                is_active=not is_deleted,
            )
        )

        db.add(
            SplynxIdMapping(
                entity_type=SplynxEntityType.tariff,
                splynx_id=mapping_id,
                dotmac_id=offer.id,
                metadata_={
                    "title": row["title"],
                    "type": "custom",
                    "price": str(price_amount),
                },
            )
        )
        mapping[row["id"]] = offer.id
        created += 1

    db.flush()
    logger.info("Tariffs (custom): %d created, %d total", created, len(mapping))
    return mapping


def migrate_tariff_availability(
    conn,
    db,
    tariff_mapping: dict[int, uuid.UUID],
    partner_mapping: dict[int, uuid.UUID],
    location_mapping: dict[int, uuid.UUID],
) -> None:
    """Migrate tariff junction tables → Offer availability.

    Idempotent: checks for existing records before inserting.
    """
    from app.models.catalog import BillingMode
    from app.models.offer_availability import (
        OfferBillingModeAvailability,
        OfferCategoryAvailability,
        OfferLocationAvailability,
        OfferResellerAvailability,
    )
    from app.models.subscriber import SubscriberCategory

    # Load existing records to avoid duplicates
    existing_reseller = {
        (r.offer_id, r.reseller_id)
        for r in db.scalars(select(OfferResellerAvailability)).all()
    }
    existing_location = {
        (r.offer_id, r.pop_site_id)
        for r in db.scalars(select(OfferLocationAvailability)).all()
    }
    existing_category = {
        (r.offer_id, r.subscriber_category.value)
        for r in db.scalars(select(OfferCategoryAvailability)).all()
    }
    existing_billing = {
        (r.offer_id, r.billing_mode.value)
        for r in db.scalars(select(OfferBillingModeAvailability)).all()
    }

    # tariffs_internet_to_partners → OfferResellerAvailability
    rows = fetch_all(conn, "SELECT * FROM tariffs_internet_to_partners")
    reseller_count = 0
    for row in rows:
        offer_id = tariff_mapping.get(row["tariff_id"])
        reseller_id = partner_mapping.get(row["partner_id"])
        if offer_id and reseller_id:
            if (offer_id, reseller_id) in existing_reseller:
                continue
            db.add(
                OfferResellerAvailability(
                    offer_id=offer_id,
                    reseller_id=reseller_id,
                )
            )
            existing_reseller.add((offer_id, reseller_id))
            reseller_count += 1
    logger.info("Offer-reseller availability: %d new", reseller_count)

    # tariffs_internet_to_locations → OfferLocationAvailability
    rows = fetch_all(conn, "SELECT * FROM tariffs_internet_to_locations")
    location_count = 0
    for row in rows:
        offer_id = tariff_mapping.get(row["tariff_id"])
        pop_site_id = location_mapping.get(row["location_id"])
        if offer_id and pop_site_id:
            if (offer_id, pop_site_id) in existing_location:
                continue
            db.add(
                OfferLocationAvailability(
                    offer_id=offer_id,
                    pop_site_id=pop_site_id,
                )
            )
            existing_location.add((offer_id, pop_site_id))
            location_count += 1
    logger.info("Offer-location availability: %d new", location_count)

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
            if (offer_id, cat.value) in existing_category:
                continue
            db.add(
                OfferCategoryAvailability(
                    offer_id=offer_id,
                    subscriber_category=cat,
                )
            )
            existing_category.add((offer_id, cat.value))
            cat_count += 1
    logger.info("Offer-category availability: %d new", cat_count)

    # tariffs_internet_to_billing_types → OfferBillingModeAvailability
    billing_map = {
        "recurring": BillingMode.postpaid,
        "prepaid": BillingMode.prepaid,
        "prepaid_monthly": BillingMode.prepaid,
    }
    rows = fetch_all(conn, "SELECT * FROM tariffs_internet_to_billing_types")
    billing_count = 0
    for row in rows:
        offer_id = tariff_mapping.get(row["tariff_id"])
        mode = billing_map.get(row.get("billing_type"))
        if offer_id and mode:
            if (offer_id, mode.value) in existing_billing:
                continue
            db.add(
                OfferBillingModeAvailability(
                    offer_id=offer_id,
                    billing_mode=mode,
                )
            )
            existing_billing.add((offer_id, mode.value))
            billing_count += 1
    logger.info("Offer-billing mode availability: %d new", billing_count)

    db.flush()


def _encrypt_if_present(value: str | None) -> str | None:
    """Encrypt a credential value if present and non-empty."""
    if not value or not value.strip():
        return None
    from app.services.credential_crypto import encrypt_credential

    return encrypt_credential(value)


def _map_nas_vendor(nas_type: str | int | None, model: str | None) -> str:
    """Map Splynx nas_type/model to NasVendor enum value."""
    if not nas_type:
        return "other"
    # Convert to string if integer
    nas_type_str = str(nas_type) if isinstance(nas_type, int) else nas_type
    nas_type_lower = nas_type_str.lower()
    if "mikrotik" in nas_type_lower or "routeros" in nas_type_lower:
        return "mikrotik"
    if "cisco" in nas_type_lower:
        return "cisco"
    if "juniper" in nas_type_lower:
        return "juniper"
    if "huawei" in nas_type_lower:
        return "huawei"
    if "ubiquiti" in nas_type_lower or "ubnt" in nas_type_lower:
        return "ubiquiti"
    # Check model string as fallback
    if model:
        model_lower = model.lower()
        if "mikrotik" in model_lower or "ccr" in model_lower or "rb" in model_lower:
            return "mikrotik"
        if "huawei" in model_lower:
            return "huawei"
    return "other"


def migrate_routers(
    conn,
    db,
    location_mapping: dict[int, uuid.UUID],
) -> dict[int, uuid.UUID]:
    """Migrate Splynx routers → NasDevice.

    Migrates:
    - Basic info: name, model, vendor
    - Network: ip_address, nas_ip
    - RADIUS: shared_secret
    - SSH credentials: login → ssh_username, password → ssh_password
    - API credentials: api_login → api_username, api_password → api_password
    - SNMP: snmp_community
    """
    from app.models.catalog import NasDevice, NasDeviceStatus, NasVendor
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    rows = fetch_all(conn, "SELECT * FROM routers ORDER BY id")
    mapping: dict[int, uuid.UUID] = {}
    created = 0
    updated = 0

    for row in rows:
        existing_mapping = db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.router,
                SplynxIdMapping.splynx_id == row["id"],
            )
        ).first()

        is_deleted = row.get("deleted") == "1"
        pop_site_id = location_mapping.get(row.get("location_id"))
        vendor_str = _map_nas_vendor(row.get("nas_type"), row.get("model"))

        # Extract credentials from Splynx (standard column names)
        ssh_username = row.get("login") or row.get("ssh_login")
        ssh_password = row.get("password") or row.get("ssh_password")
        api_username = row.get("api_login")
        api_password = row.get("api_password")
        snmp_community = row.get("snmp_community")

        if existing_mapping:
            # Update existing NAS device with any missing credentials
            nas = db.get(NasDevice, existing_mapping.dotmac_id)
            if nas:
                needs_update = False
                # Only update if credential is missing in DotMac but present in Splynx
                if not nas.ssh_username and ssh_username:
                    nas.ssh_username = ssh_username
                    needs_update = True
                if not nas.ssh_password and ssh_password:
                    nas.ssh_password = _encrypt_if_present(ssh_password)
                    needs_update = True
                if not nas.api_username and api_username:
                    nas.api_username = api_username
                    needs_update = True
                if not nas.api_password and api_password:
                    nas.api_password = _encrypt_if_present(api_password)
                    needs_update = True
                if not nas.snmp_community and snmp_community:
                    nas.snmp_community = _encrypt_if_present(snmp_community)
                    needs_update = True
                if not nas.model and row.get("model"):
                    nas.model = row.get("model")
                    needs_update = True
                if needs_update:
                    updated += 1
            mapping[row["id"]] = existing_mapping.dotmac_id
            continue

        # Create new NAS device with full credential set
        try:
            vendor_enum = NasVendor(vendor_str)
        except ValueError:
            vendor_enum = NasVendor.other

        nas = NasDevice(
            name=row["title"],
            model=row.get("model"),
            vendor=vendor_enum,
            ip_address=row.get("ip"),
            nas_ip=row.get("nas_ip"),
            pop_site_id=pop_site_id,
            shared_secret=_encrypt_if_present(row.get("radius_secret")),
            ssh_username=ssh_username,
            ssh_password=_encrypt_if_present(ssh_password),
            api_username=api_username,
            api_password=_encrypt_if_present(api_password),
            snmp_community=_encrypt_if_present(snmp_community),
            status=NasDeviceStatus.decommissioned
            if is_deleted
            else NasDeviceStatus.active,
            is_active=not is_deleted,
        )
        db.add(nas)
        db.flush()

        db.add(
            SplynxIdMapping(
                entity_type=SplynxEntityType.router,
                splynx_id=row["id"],
                dotmac_id=nas.id,
                metadata_={
                    "title": row["title"],
                    "ip": row.get("ip"),
                    "nas_ip": row.get("nas_ip"),
                    "model": row.get("model"),
                    "nas_type": row.get("nas_type"),
                    "auth_method": row.get("authorization_method"),
                    "acct_method": row.get("accounting_method"),
                    "has_ssh_creds": bool(ssh_username and ssh_password),
                    "has_api_creds": bool(api_username and api_password),
                    "deleted": is_deleted,
                },
            )
        )
        mapping[row["id"]] = nas.id
        created += 1

    db.flush()
    logger.info(
        "Routers/NAS: %d created, %d updated, %d total", created, updated, len(mapping)
    )
    return mapping


def migrate_ip_pools(conn, db) -> dict[int, uuid.UUID]:
    """Migrate Splynx ipv4_networks → IpPool + IPv4Address.

    Creates pools from network CIDRs and populates individual addresses.
    Idempotent: checks for existing pools and addresses.
    """
    import ipaddress

    from app.models.network import IpPool, IPv4Address, IPVersion
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    # Load existing addresses to avoid duplicates
    existing_addresses = {
        addr.address for addr in db.scalars(select(IPv4Address)).all()
    }

    rows = fetch_all(conn, "SELECT * FROM ipv4_networks WHERE deleted='0' ORDER BY id")
    mapping: dict[int, uuid.UUID] = {}
    pools_created = 0
    addresses_created = 0

    for row in rows:
        network = row.get("network", "")
        if not network:
            continue

        # Check if already migrated
        existing = db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.ip_network,
                SplynxIdMapping.splynx_id == row["id"],
            )
        ).first()
        if existing:
            mapping[row["id"]] = existing.dotmac_id
            continue

        # Determine CIDR — Splynx stores network without mask
        # Check for netmask column, otherwise default to /24
        netmask = row.get("netmask") or row.get("mask") or "24"
        if "/" in network:
            cidr = network
        else:
            cidr = f"{network}/{netmask}"

        name = row.get("name") or f"Pool-{row['id']}-{network}"

        pool = IpPool(
            name=name[:120],
            ip_version=IPVersion.ipv4,
            cidr=cidr,
            is_active=True,
            notes=f"Migrated from Splynx ipv4_networks.id={row['id']}",
        )
        db.add(pool)
        db.flush()

        # Create SplynxIdMapping
        db.add(
            SplynxIdMapping(
                entity_type=SplynxEntityType.ip_network,
                splynx_id=row["id"],
                dotmac_id=pool.id,
                metadata_={"network": network, "name": name},
            )
        )
        mapping[row["id"]] = pool.id
        pools_created += 1

        # Populate individual IPv4 addresses from CIDR
        try:
            ip_net = ipaddress.ip_network(cidr, strict=False)
            # Skip network and broadcast addresses, limit to /24 or smaller
            if ip_net.prefixlen >= 24:
                hosts = list(ip_net.hosts())
                for host in hosts:
                    addr_str = str(host)
                    if addr_str in existing_addresses:
                        continue  # Skip if address already exists
                    addr = IPv4Address(
                        pool_id=pool.id,
                        address=addr_str,
                        is_reserved=False,
                    )
                    db.add(addr)
                    existing_addresses.add(addr_str)
                    addresses_created += 1
            else:
                # For larger networks, just log - don't populate all addresses
                logger.info(
                    "  Large network %s (%d hosts) - not populating individual addresses",
                    cidr,
                    ip_net.num_addresses - 2,
                )
        except ValueError as e:
            logger.warning("  Invalid CIDR %s: %s", cidr, e)

    db.flush()
    logger.info(
        "IP pools: %d created, %d addresses populated", pools_created, addresses_created
    )
    return mapping


def migrate_radius_profiles(
    conn, db, tariff_mapping: dict[int, uuid.UUID]
) -> dict[int, uuid.UUID]:
    """Create RadiusProfile records from Splynx tariff speed data.

    Links profiles to offers via OfferRadiusProfile.
    """
    from app.models.catalog import (
        ConnectionType,
        NasVendor,
        OfferRadiusProfile,
        RadiusProfile,
    )
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    rows = fetch_all(conn, "SELECT * FROM tariffs_internet ORDER BY id")
    mapping: dict[int, uuid.UUID] = {}
    profiles_created = 0
    links_created = 0

    for row in rows:
        offer_id = tariff_mapping.get(row["id"])
        if not offer_id:
            continue

        # Check if profile already exists for this tariff
        existing = db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.radius_profile,
                SplynxIdMapping.splynx_id == row["id"],
            )
        ).first()
        if existing:
            mapping[row["id"]] = existing.dotmac_id
            continue

        # Extract speed data (Splynx stores in Kbps)
        speed_download = row.get("speed_download") or 0
        speed_upload = row.get("speed_upload") or 0
        burst_download = row.get("burst_download_speed") or row.get("burst_download")
        burst_upload = row.get("burst_upload_speed") or row.get("burst_upload")
        burst_threshold = row.get("burst_threshold")
        burst_time = row.get("burst_time")

        # Skip if no speed data
        if not speed_download and not speed_upload:
            continue

        # Build MikroTik rate limit string if speeds present
        # Format: rx/tx [burst_rx/burst_tx] [threshold] [time]
        rate_parts = []
        if speed_download or speed_upload:
            rate_parts.append(f"{speed_upload or 0}k/{speed_download or 0}k")
        if burst_download or burst_upload:
            rate_parts.append(f"{burst_upload or 0}k/{burst_download or 0}k")
        if burst_threshold:
            rate_parts.append(f"{burst_threshold}k/{burst_threshold}k")
        if burst_time:
            rate_parts.append(f"{burst_time}/{burst_time}")
        mikrotik_rate_limit = " ".join(rate_parts) if rate_parts else None

        profile = RadiusProfile(
            name=f"{row['title']} Profile",
            code=f"spl-{row['id']}",
            vendor=NasVendor.mikrotik,  # Default to MikroTik for Splynx
            connection_type=ConnectionType.pppoe,
            download_speed=speed_download or None,
            upload_speed=speed_upload or None,
            burst_download=burst_download,
            burst_upload=burst_upload,
            burst_threshold=burst_threshold,
            burst_time=burst_time,
            ip_pool_name=row.get("pool_name") or row.get("pool"),
            mikrotik_rate_limit=mikrotik_rate_limit,
            simultaneous_use=row.get("simultaneous_sessions") or 1,
            is_active=row.get("deleted") != "1",
        )
        db.add(profile)
        db.flush()

        # Create mapping
        db.add(
            SplynxIdMapping(
                entity_type=SplynxEntityType.radius_profile,
                splynx_id=row["id"],
                dotmac_id=profile.id,
                metadata_={
                    "tariff_title": row["title"],
                    "speed_down": speed_download,
                    "speed_up": speed_upload,
                },
            )
        )
        mapping[row["id"]] = profile.id
        profiles_created += 1

        # Link profile to offer
        link = OfferRadiusProfile(
            offer_id=offer_id,
            profile_id=profile.id,
        )
        db.add(link)
        links_created += 1

    db.flush()
    logger.info(
        "RADIUS profiles: %d created, %d linked to offers",
        profiles_created,
        links_created,
    )
    return mapping


def migrate_fup_policies(conn, db, tariff_mapping: dict[int, uuid.UUID]) -> None:
    """Migrate Splynx FUP settings from tariffs_internet to FupPolicy/FupRule.

    Splynx stores FUP config in tariffs_internet table:
    - fup_policy_id, fup_rules (JSON), fup_cap_*, etc.
    """
    from app.models.fup import (
        FupAction,
        FupConsumptionPeriod,
        FupDataUnit,
        FupDirection,
        FupPolicy,
        FupRule,
    )

    rows = fetch_all(conn, "SELECT * FROM tariffs_internet ORDER BY id")
    policies_created = 0
    rules_created = 0

    for row in rows:
        offer_id = tariff_mapping.get(row["id"])
        if not offer_id:
            continue

        # Check if this tariff has FUP data
        # Splynx uses various column names depending on version
        fup_enabled = row.get("fup_enabled") == "1" or row.get("fup") == "1"
        fup_cap_download = row.get("fup_cap_download") or row.get("fup_download")
        fup_cap_upload = row.get("fup_cap_upload") or row.get("fup_upload")
        fup_cap_total = row.get("fup_cap_total") or row.get("fup_traffic")
        fup_speed_percent = row.get("fup_speed_percent") or row.get(
            "fup_speed_reduction"
        )

        # Skip if no FUP config
        if not fup_enabled and not any(
            [fup_cap_download, fup_cap_upload, fup_cap_total]
        ):
            continue

        # Check if policy already exists for this offer
        existing_policy = db.scalars(
            select(FupPolicy).where(FupPolicy.offer_id == offer_id)
        ).first()
        if existing_policy:
            continue

        # Create FUP policy
        policy = FupPolicy(
            offer_id=offer_id,
            is_active=fup_enabled,
            notes=f"Migrated from Splynx tariff {row['id']}",
        )
        db.add(policy)
        db.flush()
        policies_created += 1

        # Create FUP rules based on cap data
        rule_order = 0

        # Download cap rule
        if fup_cap_download:
            try:
                cap_gb = float(fup_cap_download) / 1024  # MB to GB
                if cap_gb > 0:
                    rule = FupRule(
                        policy_id=policy.id,
                        name=f"Download Cap {cap_gb:.1f} GB",
                        sort_order=rule_order,
                        consumption_period=FupConsumptionPeriod.monthly,
                        direction=FupDirection.down,
                        threshold_amount=cap_gb,
                        threshold_unit=FupDataUnit.gb,
                        action=FupAction.reduce_speed,
                        speed_reduction_percent=float(fup_speed_percent or 50),
                        is_active=True,
                    )
                    db.add(rule)
                    rules_created += 1
                    rule_order += 1
            except (ValueError, TypeError):
                pass

        # Upload cap rule
        if fup_cap_upload:
            try:
                cap_gb = float(fup_cap_upload) / 1024
                if cap_gb > 0:
                    rule = FupRule(
                        policy_id=policy.id,
                        name=f"Upload Cap {cap_gb:.1f} GB",
                        sort_order=rule_order,
                        consumption_period=FupConsumptionPeriod.monthly,
                        direction=FupDirection.up,
                        threshold_amount=cap_gb,
                        threshold_unit=FupDataUnit.gb,
                        action=FupAction.reduce_speed,
                        speed_reduction_percent=float(fup_speed_percent or 50),
                        is_active=True,
                    )
                    db.add(rule)
                    rules_created += 1
                    rule_order += 1
            except (ValueError, TypeError):
                pass

        # Total cap rule
        if fup_cap_total:
            try:
                cap_gb = float(fup_cap_total) / 1024
                if cap_gb > 0:
                    rule = FupRule(
                        policy_id=policy.id,
                        name=f"Total Cap {cap_gb:.1f} GB",
                        sort_order=rule_order,
                        consumption_period=FupConsumptionPeriod.monthly,
                        direction=FupDirection.up_down,
                        threshold_amount=cap_gb,
                        threshold_unit=FupDataUnit.gb,
                        action=FupAction.reduce_speed,
                        speed_reduction_percent=float(fup_speed_percent or 50),
                        is_active=True,
                    )
                    db.add(rule)
                    rules_created += 1
            except (ValueError, TypeError):
                pass

    db.flush()
    logger.info("FUP policies: %d created, %d rules", policies_created, rules_created)


def run_phase0(dry_run: bool = True) -> None:
    """Execute Phase 0 migration."""
    logger.info("=== Phase 0: Reference Data Migration ===")

    with splynx_connection() as conn:
        with dotmac_session() as db:
            if dry_run:
                logger.info("DRY RUN — counting source data only")
                tables = [
                    (
                        "locations",
                        "SELECT COUNT(*) as cnt FROM locations WHERE deleted='0'",
                    ),
                    ("partners (all)", "SELECT COUNT(*) as cnt FROM partners"),
                    (
                        "partners (deleted)",
                        "SELECT COUNT(*) as cnt FROM partners WHERE deleted='1'",
                    ),
                    ("tax", "SELECT COUNT(*) as cnt FROM tax"),
                    (
                        "tariffs_internet",
                        "SELECT COUNT(*) as cnt FROM tariffs_internet",
                    ),
                    ("tariffs_custom", "SELECT COUNT(*) as cnt FROM tariffs_custom"),
                    ("routers", "SELECT COUNT(*) as cnt FROM routers"),
                    (
                        "ipv4_networks",
                        "SELECT COUNT(*) as cnt FROM ipv4_networks WHERE deleted='0'",
                    ),
                    (
                        "tariffs_to_partners",
                        "SELECT COUNT(*) as cnt FROM tariffs_internet_to_partners",
                    ),
                    (
                        "tariffs_to_locations",
                        "SELECT COUNT(*) as cnt FROM tariffs_internet_to_locations",
                    ),
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
                conn,
                db,
                tariff_mapping,
                partner_mapping,
                location_mapping,
            )
            router_mapping = migrate_routers(conn, db, location_mapping)
            ip_pool_mapping = migrate_ip_pools(conn, db)

            # Migrate RADIUS profiles from tariff speed data
            radius_profile_mapping = migrate_radius_profiles(conn, db, tariff_mapping)

            # Migrate FUP policies from tariff FUP settings
            migrate_fup_policies(conn, db, tariff_mapping)

            db.commit()
            logger.info("=== Phase 0 complete — committed ===")

            # Summary
            from sqlalchemy import func

            from app.models.splynx_mapping import SplynxIdMapping

            counts = db.execute(
                select(
                    SplynxIdMapping.entity_type, func.count(SplynxIdMapping.id)
                ).group_by(SplynxIdMapping.entity_type)
            ).all()
            logger.info("--- SplynxIdMapping summary ---")
            for entity_type, count in counts:
                logger.info("  %s: %d", entity_type.value, count)


if __name__ == "__main__":
    if "--execute" in sys.argv:
        run_phase0(dry_run=False)
    else:
        run_phase0(dry_run=True)
        print(
            "\nTo execute: poetry run python scripts/migration/phase0_reference_data.py --execute"
        )
