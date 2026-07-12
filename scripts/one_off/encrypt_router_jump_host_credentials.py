"""Encrypt existing plaintext Router and JumpHost credentials.

Router and JumpHost joined ``ENCRYPTED_MODEL_FIELDS`` when scheduled credential
rotation landed, which wired encryption into the *write* path
(``RouterInventory``/``JumpHostInventory`` create+update) but left rows written
before that change as plaintext at rest. Reads still work — ``decrypt_credential``
passes unprefixed legacy values through untouched — so nothing is broken; the
values are simply not encrypted yet.

Two things would eventually convert them: editing each router/jump host, or a key
rotation (``_rotate_value`` encrypts non-``enc:`` values). Neither is dependable —
the first only covers rows someone happens to touch, and the second is blocked
outright while ``CREDENTIAL_ENCRYPTION_KEY`` is a literal environment value
(``_managed_key_source`` -> ``static_environment_key``). This script does the
backfill explicitly instead, in the same shape as ``encrypt_nas_credentials.py``.

Usage:
    # Dry run (show what would be changed)
    python scripts/one_off/encrypt_router_jump_host_credentials.py --dry-run

    # Execute encryption
    python scripts/one_off/encrypt_router_jump_host_credentials.py --execute

Requirements:
    - CREDENTIAL_ENCRYPTION_KEY must resolve to a real key before running.
      Without one, encrypt_credential() falls back to a "plain:" prefix, which
      would rewrite every row without actually encrypting anything.
"""

import argparse
import sys

from dotenv import load_dotenv

from app.db import SessionLocal
from app.models.router_management import JumpHost, Router
from app.services.credential_crypto import (
    ENCRYPTED_MODEL_FIELDS,
    encrypt_credential,
    get_encryption_key,
    is_encrypted,
)
from app.services.secrets import is_secret_ref

# Driven off the source-of-truth map rather than a local copy, so adding a field
# there can't silently leave this backfill behind.
_TARGETS: tuple[tuple[type, tuple[str, ...]], ...] = (
    (Router, ENCRYPTED_MODEL_FIELDS["Router"]),
    (JumpHost, ENCRYPTED_MODEL_FIELDS["JumpHost"]),
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Encrypt existing Router and JumpHost credentials at rest."
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


def _label(row) -> str:
    return getattr(row, "name", None) or getattr(row, "hostname", None) or str(row.id)


def main():
    load_dotenv()
    args = parse_args()

    if not get_encryption_key():
        print("ERROR: no credential encryption key is configured.")
        print(
            "Refusing to run: encrypt_credential() would fall back to a 'plain:' "
            "prefix and rewrite every row without encrypting it."
        )
        print("Set CREDENTIAL_ENCRYPTION_KEY (or the auth domain setting) first.")
        sys.exit(1)

    stats = {
        "rows_checked": 0,
        "rows_updated": 0,
        "encrypted": 0,
        "already_encrypted": 0,
        "secret_refs_skipped": 0,
        "empty_fields": 0,
    }

    db = SessionLocal()
    try:
        for model, fields in _TARGETS:
            rows = db.query(model).all()
            print(f"{model.__name__}: {len(rows)} row(s), fields={list(fields)}")

            for row in rows:
                stats["rows_checked"] += 1
                changed: list[str] = []

                for field in fields:
                    value = getattr(row, field, None)

                    if not value:
                        stats["empty_fields"] += 1
                        continue
                    if is_encrypted(value):
                        stats["already_encrypted"] += 1
                        continue
                    if is_secret_ref(value):
                        # A bao:// reference is not plaintext; encrypt_credential
                        # would hand it straight back anyway.
                        stats["secret_refs_skipped"] += 1
                        continue

                    changed.append(field)
                    stats["encrypted"] += 1
                    if args.execute:
                        setattr(row, field, encrypt_credential(value))

                if changed:
                    verb = "would encrypt" if args.dry_run else "encrypted"
                    print(f"  {model.__name__} '{_label(row)}': {verb} {changed}")
                    stats["rows_updated"] += 1

        if args.execute:
            db.commit()
        else:
            db.rollback()
    finally:
        db.close()

    print()
    print("Summary:")
    print(f"  Rows checked:        {stats['rows_checked']}")
    print(f"  Rows updated:        {stats['rows_updated']}")
    print(f"  Credentials encrypted: {stats['encrypted']}")
    print(f"  Already encrypted:   {stats['already_encrypted']}")
    print(f"  Secret refs skipped: {stats['secret_refs_skipped']}")
    print(f"  Empty fields:        {stats['empty_fields']}")

    if args.dry_run and stats["encrypted"]:
        print()
        print("To apply these changes, re-run with --execute")


if __name__ == "__main__":
    main()
