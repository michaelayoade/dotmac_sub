#!/usr/bin/env python3
"""Bulk-encrypt plaintext PPPoE passwords stored in ont_units.

After SmartOLT sync stored passwords in plaintext, this script encrypts
them using the project's credential_crypto module (Fernet encryption).

Usage:
    # Dry run — shows how many passwords need encryption
    poetry run python scripts/encrypt_ont_passwords.py

    # Execute encryption
    poetry run python scripts/encrypt_ont_passwords.py --execute
"""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, ".")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("encrypt_passwords")


def run(*, dry_run: bool = True) -> dict[str, int]:
    """Encrypt all plaintext PPPoE passwords in ont_units.

    Returns:
        Stats dict: {total, encrypted, already_encrypted, empty, errors}.
    """
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.network import OntUnit
    from app.services.credential_crypto import encrypt_credential, is_encrypted

    db = SessionLocal()
    stats = {
        "total": 0,
        "encrypted": 0,
        "already_encrypted": 0,
        "empty": 0,
        "errors": 0,
    }

    try:
        onts = db.scalars(
            select(OntUnit).where(OntUnit.pppoe_password.isnot(None))
        ).all()

        stats["total"] = len(onts)
        logger.info("Found %d ONTs with pppoe_password set", len(onts))

        for ont in onts:
            pw = ont.pppoe_password
            if not pw or pw.strip() == "":
                stats["empty"] += 1
                continue

            if is_encrypted(pw):
                stats["already_encrypted"] += 1
                continue

            try:
                encrypted = encrypt_credential(pw)
                if not dry_run:
                    ont.pppoe_password = encrypted
                stats["encrypted"] += 1
            except Exception as exc:
                logger.warning(
                    "Failed to encrypt password for ONT %s: %s",
                    ont.serial_number,
                    exc,
                )
                stats["errors"] += 1

        if not dry_run:
            db.commit()
            logger.info("Committed %d encrypted passwords", stats["encrypted"])
        else:
            logger.info("[DRY RUN] Would encrypt %d passwords", stats["encrypted"])

    except Exception as exc:
        logger.error("Bulk encryption failed: %s", exc)
        db.rollback()
        raise
    finally:
        db.close()

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk-encrypt plaintext PPPoE passwords in ont_units",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually encrypt (default is dry-run)",
    )
    args = parser.parse_args()

    dry_run = not args.execute
    mode = "DRY RUN" if dry_run else "EXECUTE"
    logger.info("=== Bulk PPPoE Password Encryption [%s] ===", mode)

    stats = run(dry_run=dry_run)

    logger.info("")
    logger.info("=== Summary ===")
    logger.info("Total with password:  %d", stats["total"])
    logger.info("Already encrypted:    %d", stats["already_encrypted"])
    logger.info("Encrypted (this run): %d", stats["encrypted"])
    logger.info("Empty/blank:          %d", stats["empty"])
    logger.info("Errors:               %d", stats["errors"])

    if dry_run:
        logger.info("")
        logger.info("This was a DRY RUN. Add --execute to encrypt.")


if __name__ == "__main__":
    main()
