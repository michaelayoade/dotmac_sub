#!/usr/bin/env python3
"""Bootstrap database-local prerequisites for disposable PostgreSQL tests.

The integration gate first prepares the explicit TEST_DATABASE_URL, then many
migration rehearsal tests create additional databases with PostgreSQL's default
``CREATE DATABASE`` behaviour. Those databases inherit database-local schema ACLs
from ``template1``, not from TEST_DATABASE_URL. Production deploys run the same
bootstrap before migrations; this CI adapter applies that contract to both
places in a validated disposable test cluster.
"""

from __future__ import annotations

import os
import sys

import psycopg
from sqlalchemy.engine import URL

from scripts.bootstrap_commercial_module_prereqs import (
    bootstrap as bootstrap_commercial_module_prereqs,
)
from scripts.bootstrap_outbox_dispatcher_roles import (
    bootstrap as bootstrap_outbox_dispatcher_roles,
)
from scripts.ci.migrated_test_database import (
    DatabaseContractError,
    parse_test_database_target,
)


def _psycopg_url(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _bootstrap_url(url: URL, *, label: str) -> int:
    with psycopg.connect(_psycopg_url(url), autocommit=False) as conn:
        commercial_result = bootstrap_commercial_module_prereqs(
            conn, dry_run=False, repair=True
        )
        if commercial_result != 0:
            print(
                f"failed commercial module prerequisite bootstrap for {label}",
                file=sys.stderr,
            )
            return commercial_result
        outbox_result = bootstrap_outbox_dispatcher_roles(
            conn, dry_run=False, repair=True
        )
        if outbox_result != 0:
            print(
                f"failed outbox dispatcher prerequisite bootstrap for {label}",
                file=sys.stderr,
            )
            return outbox_result
    print(f"bootstrapped database-local prerequisites for {label}")
    return 0


def main() -> int:
    try:
        target = parse_test_database_target(os.getenv("TEST_DATABASE_URL"))
    except DatabaseContractError as exc:
        print(f"REFUSED [{exc.code.value}] {exc}", file=sys.stderr)
        return 2

    for label, url in (
        (target.database_name, target.url),
        ("template1", target.url.set(database="template1")),
    ):
        result = _bootstrap_url(url, label=label)
        if result != 0:
            return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
