import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.services.settings_secret_cleanup import migrate_plaintext_secret_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate plaintext secret settings into OpenBao refs."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write secrets to OpenBao and update DB refs. Default is dry-run.",
    )
    parser.add_argument("--domain", help="Limit to a specific setting domain.")
    parser.add_argument("--key", help="Limit to a specific setting key.")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()
    db: Session = SessionLocal()
    try:
        result = migrate_plaintext_secret_settings(
            db,
            dry_run=not args.apply,
            domain=args.domain,
            key=args.key,
        )
    finally:
        db.close()

    mode = "apply" if args.apply else "dry-run"
    print(
        f"Settings secret cleanup ({mode}) complete. "
        f"migrated={result.migrated} skipped={result.skipped}"
    )
    if result.migrated_keys:
        print("Migrated candidates:")
        for item in result.migrated_keys:
            print(f"- {item}")
    if result.skipped_keys:
        print("Skipped:")
        for item in result.skipped_keys:
            print(f"- {item}")
    if result.errors:
        print("Errors:")
        for item in result.errors:
            print(f"- {item}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
