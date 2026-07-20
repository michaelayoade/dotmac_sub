"""Migrate noncanonical secret settings to OpenBao references.

The command is a dry-run unless ``--apply`` is supplied. It reports only
domain/key identifiers and counts; secret values are never printed.

Usage:

    python -m scripts.one_off.migrate_secret_settings_to_openbao
    python -m scripts.one_off.migrate_secret_settings_to_openbao --domain auth
    python -m scripts.one_off.migrate_secret_settings_to_openbao --apply
"""

from __future__ import annotations

import argparse

from app.db import SessionLocal
from app.services.settings_secret_cleanup import (
    SecretCleanupResult,
    migrate_plaintext_secret_settings,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write OpenBao values and replace settings with bao:// refs.",
    )
    parser.add_argument("--domain", help="Restrict the migration to one domain.")
    parser.add_argument("--key", help="Restrict the migration to one setting key.")
    return parser


def _print_result(result: SecretCleanupResult, *, dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "APPLIED"
    print(
        f"{mode}: migrated={result.migrated} "
        f"skipped={result.skipped} errors={len(result.errors)}"
    )
    for key_name in result.migrated_keys:
        verb = "would migrate" if dry_run else "migrated"
        print(f"{verb}: {key_name}")
    for key_name in result.skipped_keys:
        print(f"skipped: {key_name}")
    for error in result.errors:
        print(f"error: {error}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dry_run = not args.apply
    with SessionLocal() as db:
        result = migrate_plaintext_secret_settings(
            db,
            dry_run=dry_run,
            domain=args.domain,
            key=args.key,
        )
    _print_result(result, dry_run=dry_run)
    if dry_run and result.migrated:
        print("Review the identifiers above, then rerun with --apply to write.")
    return 1 if result.errors else 0


if __name__ == "__main__":  # pragma: no cover - exercised by operators
    raise SystemExit(main())
