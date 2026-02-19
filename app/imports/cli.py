from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.db import SessionLocal
from app.imports.loader import ImportError
from app.services import imports as import_service


def _coerce_error_index(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def import_subscriber_custom_fields(path: str) -> int:
    errors: list[ImportError] = []
    db = SessionLocal()
    try:
        content = Path(path).expanduser().read_text(encoding="utf-8")
        created, service_errors = import_service.import_subscriber_custom_fields_from_csv(
            db, content
        )
        errors.extend(
            ImportError(
                index=_coerce_error_index(err.get("index")),
                detail=str(err.get("detail")),
            )
            for err in service_errors
        )
    finally:
        db.close()
    print(f"created={created} errors={len(errors)}")
    for err in errors:
        print(f"row={err.index} error={err.detail}", file=sys.stderr)
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Import CSV data into dotmac_sm")
    parser.add_argument(
        "resource",
        choices=["subscriber-custom-fields"],
        help="Resource to import from CSV",
    )
    parser.add_argument("path", help="Path to CSV file")
    args = parser.parse_args()

    if args.resource == "subscriber-custom-fields":
        raise SystemExit(import_subscriber_custom_fields(args.path))


if __name__ == "__main__":
    main()
