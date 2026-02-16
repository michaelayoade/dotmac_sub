"""Encrypt existing plaintext NAS device credentials.

This script migrates existing NAS device credentials from plaintext to encrypted format.
It should be run once after deploying the credential encryption feature.

Usage:
    # Dry run (show what would be changed)
    python scripts/encrypt_nas_credentials.py --dry-run

    # Execute encryption
    python scripts/encrypt_nas_credentials.py --execute

Requirements:
    - Set CREDENTIAL_ENCRYPTION_KEY environment variable before running
    - Or configure credential_encryption_key in security domain settings
"""

import argparse
import sys

from dotenv import load_dotenv

from app.db import SessionLocal
from app.models.catalog import NasDevice
from app.services.credential_crypto import (
    ENCRYPTED_CREDENTIAL_FIELDS,
    encrypt_credential,
    get_encryption_key,
    is_encrypted,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Encrypt existing NAS device credentials."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be encrypted without making changes",
    )
    group.add_argument(
        "--execute",
        action="store_true",
        help="Actually encrypt credentials in the database",
    )
    return parser.parse_args()


def main():
    load_dotenv()
    args = parse_args()

    # Verify encryption key is available
    key = get_encryption_key()
    if not key:
        print("ERROR: CREDENTIAL_ENCRYPTION_KEY is not configured.")
        print("Please set the environment variable before running this script.")
        print()
        print("Generate a new key with:")
        print('  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"')
        sys.exit(1)

    db = SessionLocal()
    try:
        devices = db.query(NasDevice).all()
        print(f"Found {len(devices)} NAS devices")
        print()

        stats = {
            "devices_checked": 0,
            "devices_updated": 0,
            "credentials_encrypted": 0,
            "already_encrypted": 0,
            "empty_fields": 0,
        }

        for device in devices:
            stats["devices_checked"] += 1
            device_needs_update = False
            device_changes = []

            for field in ENCRYPTED_CREDENTIAL_FIELDS:
                value = getattr(device, field, None)
                if not value:
                    stats["empty_fields"] += 1
                    continue

                if is_encrypted(value):
                    stats["already_encrypted"] += 1
                    continue

                # This credential needs encryption
                device_changes.append(field)
                if args.execute:
                    encrypted_value = encrypt_credential(value)
                    setattr(device, field, encrypted_value)
                    device_needs_update = True
                    stats["credentials_encrypted"] += 1

            if device_changes:
                prefix = "[DRY RUN] " if args.dry_run else ""
                print(f"{prefix}Device '{device.name}' (ID: {device.id}):")
                for field in device_changes:
                    print(f"  - {field}: would be encrypted" if args.dry_run else f"  - {field}: encrypted")
                    if args.dry_run:
                        stats["credentials_encrypted"] += 1
                stats["devices_updated"] += 1

            if device_needs_update and args.execute:
                db.commit()

        print()
        print("Summary:")
        print(f"  Devices checked: {stats['devices_checked']}")
        print(f"  Devices {'would be ' if args.dry_run else ''}updated: {stats['devices_updated']}")
        print(f"  Credentials {'would be ' if args.dry_run else ''}encrypted: {stats['credentials_encrypted']}")
        print(f"  Already encrypted: {stats['already_encrypted']}")
        print(f"  Empty fields: {stats['empty_fields']}")

        if args.dry_run and stats["credentials_encrypted"] > 0:
            print()
            print("To apply these changes, run with --execute flag")

    finally:
        db.close()


if __name__ == "__main__":
    main()
