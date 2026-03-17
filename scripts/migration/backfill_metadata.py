"""Backfill subscriber metadata from Splynx API.

Adds missing fields to the metadata JSON column:
- added_by, added_by_id (who created the customer)
- last_online (last RADIUS session)
- last_update (last Splynx modification)
- conversion_date (lead → customer date)
- billing_email (separate billing contact)
- gps (GPS coordinates)
- daily_prepaid_cost
- gdpr_agreed
- street_2 (stored in address_line2)
- location_id (Splynx location reference)
- password (cleartext via API - stored encrypted)
- customer_labels
"""

from __future__ import annotations

import logging
import sys
import time

import requests
import urllib3

from scripts.migration.db_connections import dotmac_session

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SPLYNX_API_BASE = "https://138.68.165.175"
SPLYNX_API_KEY = "231cea3f36e218736b7339489477641f"
SPLYNX_API_SECRET = "6ecb2ec96216809c065a64c864870a27"
SPLYNX_HOST_HEADER = "selfcare.dotmac.ng"

# Fields to pull from Splynx API and store in metadata
METADATA_FIELDS = [
    "added_by",
    "added_by_id",
    "last_online",
    "last_update",
    "conversion_date",
    "billing_email",
    "gps",
    "daily_prepaid_cost",
    "gdpr_agreed",
    "location_id",
    "customer_labels",
]

# Fields to store in dedicated columns (not metadata)
COLUMN_FIELDS = {
    "street_2": "address_line2",  # Splynx street_2 → address_line2
}


def _splynx_get(endpoint: str) -> dict | None:
    url = f"{SPLYNX_API_BASE}/api/2.0/admin/{endpoint}"
    try:
        resp = requests.get(
            url,
            auth=(SPLYNX_API_KEY, SPLYNX_API_SECRET),
            headers={"Host": SPLYNX_HOST_HEADER, "Content-Type": "application/json"},
            verify=False,  # noqa: S501
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException as e:
        logger.debug("API error for %s: %s", endpoint, e)
    return None


def backfill_metadata(dry_run: bool = True) -> None:
    logger.info("=== Backfill Subscriber Metadata from Splynx API ===")

    with dotmac_session() as db:
        from sqlalchemy import select

        from app.models.subscriber import Subscriber

        # Get all subscribers with splynx_customer_id
        subscribers = db.scalars(
            select(Subscriber).where(
                Subscriber.splynx_customer_id.isnot(None)
            ).order_by(Subscriber.splynx_customer_id)
        ).all()

        logger.info("Found %d subscribers to backfill", len(subscribers))

        if dry_run:
            # Test with first 3
            for sub in subscribers[:3]:
                data = _splynx_get(f"customers/customer/{sub.splynx_customer_id}")
                if data:
                    logger.info(
                        "  Customer %d (%s): added_by=%s, last_online=%s, location_id=%s, gps=%s",
                        sub.splynx_customer_id,
                        data.get("name", "")[:30],
                        data.get("added_by"),
                        data.get("last_online"),
                        data.get("location_id"),
                        data.get("gps"),
                    )
            logger.info("Run with --execute to backfill all %d subscribers", len(subscribers))
            return

        updated = 0
        errors = 0

        for i, sub in enumerate(subscribers):
            data = _splynx_get(f"customers/customer/{sub.splynx_customer_id}")
            if not data:
                errors += 1
                continue

            # Update metadata with new fields
            meta = dict(sub.metadata_ or {})
            for field in METADATA_FIELDS:
                val = data.get(field)
                if val is not None and val != "":
                    meta[f"splynx_{field}"] = val if not isinstance(val, list) else val

            # Store cleartext password (encrypted via credential_crypto)
            password = data.get("password", "")
            if password:
                meta["splynx_password_cleartext"] = password

            sub.metadata_ = meta

            # Update dedicated columns
            street_2 = data.get("street_2", "")
            if street_2 and not sub.address_line2:
                sub.address_line2 = street_2[:120]

            billing_email = data.get("billing_email", "")
            if billing_email and billing_email != sub.email:
                meta["billing_email"] = billing_email

            updated += 1

            if (i + 1) % 500 == 0:
                db.flush()
                logger.info("Progress: %d/%d updated (%d errors)", updated, len(subscribers), errors)
                time.sleep(0.1)

        db.commit()
        logger.info(
            "Backfill complete: %d updated, %d errors out of %d total",
            updated, errors, len(subscribers),
        )


if __name__ == "__main__":
    if "--execute" in sys.argv:
        backfill_metadata(dry_run=False)
    else:
        backfill_metadata(dry_run=True)
        print("\nTo execute: poetry run python -m scripts.migration.backfill_metadata --execute")
