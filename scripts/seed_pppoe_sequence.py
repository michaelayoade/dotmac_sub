"""Seed the PPPoE DocumentSequence from Splynx's current MAX(customer_id).

Queries the live Splynx database to determine the highest customer_id
and initialises the DotMac ``pppoe_username`` DocumentSequence so that
new PPPoE usernames continue the same ``1000xxxxx`` series without gaps
or collisions.

Usage::

    # Auto-detect from Splynx (recommended at cutover)
    poetry run python scripts/seed_pppoe_sequence.py

    # Manual override
    poetry run python scripts/seed_pppoe_sequence.py --start-value 26000

    # Overwrite existing sequence
    poetry run python scripts/seed_pppoe_sequence.py --force
"""

from __future__ import annotations

import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

SEQUENCE_KEY = "pppoe_username"


def _fetch_splynx_max_customer_id() -> int:
    """Query Splynx MySQL for the current MAX(customer_id)."""
    from scripts.migration.db_connections import fetch_all, splynx_connection

    with splynx_connection() as conn:
        rows = fetch_all(conn, "SELECT MAX(id) AS max_id FROM customers")
        max_id = rows[0]["max_id"]
        logger.info("Splynx MAX(customer_id) = %s", max_id)
        return int(max_id)


def seed_pppoe_sequence(
    *,
    start_value: int | None = None,
    force: bool = False,
) -> str:
    """Initialise the PPPoE DocumentSequence.

    Args:
        start_value: Explicit next sequence value.  If None, queries
            Splynx for MAX(customer_id) + 1.
        force: Overwrite an existing sequence value.

    Returns:
        Human-readable status message.
    """
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.sequence import DocumentSequence

    if start_value is None:
        max_id = _fetch_splynx_max_customer_id()
        start_value = max_id + 1
        logger.info("Computed start_value = %d (MAX + 1)", start_value)

    db = SessionLocal()
    try:
        existing = db.scalars(
            select(DocumentSequence).where(DocumentSequence.key == SEQUENCE_KEY)
        ).first()
        if existing and not force:
            return (
                f"Sequence '{SEQUENCE_KEY}' already exists with "
                f"next_value={existing.next_value}. Use --force to overwrite."
            )

        if existing:
            old_value = existing.next_value
            existing.next_value = start_value
            db.commit()
            return (
                f"PPPoE sequence '{SEQUENCE_KEY}' updated: "
                f"{old_value} → {start_value}"
            )

        db.add(DocumentSequence(key=SEQUENCE_KEY, next_value=start_value))
        db.commit()
        return f"PPPoE sequence '{SEQUENCE_KEY}' created with start_value={start_value}"
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed PPPoE username sequence from Splynx MAX(customer_id).",
    )
    parser.add_argument(
        "--start-value",
        type=int,
        default=None,
        help="Next sequence value (default: auto-detect from Splynx MAX + 1)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing sequence value",
    )
    args = parser.parse_args()
    result = seed_pppoe_sequence(start_value=args.start_value, force=args.force)
    print(result)


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    main()
