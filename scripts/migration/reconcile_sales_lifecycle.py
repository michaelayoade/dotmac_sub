#!/usr/bin/env python3
"""Report or repair sales-to-service projection drift through domain owners."""

from __future__ import annotations

import argparse
import json

from app.db import SessionLocal
from app.services.sales_lifecycle_reconciliation import (
    reconcile_sales_to_service_lifecycle,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Request idempotent repairs through the canonical owners",
    )
    args = parser.parse_args()
    with SessionLocal() as db:
        result = reconcile_sales_to_service_lifecycle(db, apply=args.apply)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
