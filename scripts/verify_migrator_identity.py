#!/usr/bin/env python3
"""Read-only check that this connection is the contract's migration identity.

``scripts/deploy.sh`` runs this as a one-off with the migration credential
BEFORE it pins an image, so a role-graph mismatch refuses the deploy while
nothing has changed. It runs the same check ``alembic/env.py`` repeats when it
migrates (``app.migration_ownership.assert_migrator_identity``): the connection
is the BYPASSRLS non-superuser migrator, a member of the object owner with
INHERIT and SET, and the owner does not reach the migrator.

Exit codes: 0 satisfied, 1 contract violation, 2 usage or connection error.
Nothing is written and no connection detail is printed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.migration_ownership import (
    OWNER_ENV_VAR,
    MigrationOwnershipError,
    assert_migrator_identity,
)


def main() -> int:
    owner = os.environ.get(OWNER_ENV_VAR, "").strip()
    if not owner:
        print(f"{OWNER_ENV_VAR} is not set", file=sys.stderr)
        return 2
    engine = create_engine(settings.alembic_database_url)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            try:
                assert_migrator_identity(connection, owner=owner)
            except MigrationOwnershipError as exc:
                print(f"MIGRATOR IDENTITY REFUSED: {exc}", file=sys.stderr)
                return 1
            connection.rollback()
    except SQLAlchemyError:
        print(
            "database connection or identity query failed; connection details "
            "were deliberately not logged",
            file=sys.stderr,
        )
        return 2
    print("MIGRATOR IDENTITY: satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
