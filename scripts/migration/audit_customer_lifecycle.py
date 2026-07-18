#!/usr/bin/env python3
"""Report aggregate Party-to-support customer lifecycle convergence."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services.customer_lifecycle_audit import build_customer_lifecycle_audit


def _set_transaction_read_only(db: Session) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


def main() -> int:
    with SessionLocal() as db:
        _set_transaction_read_only(db)
        result = build_customer_lifecycle_audit(db)
        db.rollback()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "installed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
