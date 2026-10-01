#!/usr/bin/env python3
"""Read-only proof that runtime and migration logins reach one PostgreSQL backend."""

from __future__ import annotations

import ipaddress
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

import psycopg

from app.commercial_module_prereqs import (
    COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT,
    MigrationPrincipalObservation,
    migration_principal_is_valid,
)

_CATALOG_SQL = """
SELECT session_user, current_user, principal.rolcanlogin,
       principal.rolbypassrls, principal.rolsuper, principal.rolcreatedb,
       principal.rolcreaterole,
       has_database_privilege(session_user, current_database(), 'CREATE'),
       current_database(), host(inet_server_addr()), inet_server_port(),
       pg_postmaster_start_time()
  FROM pg_roles AS principal
 WHERE principal.rolname = session_user
"""


@dataclass(frozen=True)
class BackendIdentity:
    database: str
    address: str
    port: int
    postmaster_started_at: datetime

    def __post_init__(self) -> None:
        if (
            type(self.database) is not str
            or type(self.address) is not str
            or type(self.port) is not int
            or not self.database
            or not self.address
            or not (1 <= self.port <= 65535)
        ):
            raise ValueError("incomplete backend identity")
        if not isinstance(self.postmaster_started_at, datetime):
            raise ValueError("incomplete backend identity")
        if self.postmaster_started_at.tzinfo is None or (
            self.postmaster_started_at.utcoffset() is None
        ):
            raise ValueError("postmaster time must be timezone aware")
        ipaddress.ip_address(self.address)


@dataclass(frozen=True)
class ConnectionObservation:
    principal: MigrationPrincipalObservation
    backend: BackendIdentity


def decode_observation(row: Sequence[object] | None) -> ConnectionObservation | None:
    """Decode the fixed catalog row into named immutable evidence."""

    if row is None or len(row) != 12:
        return None
    try:
        principal = MigrationPrincipalObservation(
            session_user=cast(str, row[0]),  # DTO validates exact runtime types
            current_user=cast(str, row[1]),
            can_login=cast(bool, row[2]),
            bypass_rls=cast(bool, row[3]),
            superuser=cast(bool, row[4]),
            can_create_database=cast(bool, row[5]),
            can_create_role=cast(bool, row[6]),
            database_create=cast(bool, row[7]),
        )
        if (
            type(row[8]) is not str
            or type(row[9]) is not str
            or type(row[10]) is not int
            or not isinstance(row[11], datetime)
        ):
            return None
        backend = BackendIdentity(
            database=row[8],
            address=row[9],
            port=row[10],
            postmaster_started_at=row[11],
        )
    except (ValueError, TypeError):
        return None
    return ConnectionObservation(principal=principal, backend=backend)


def pair_refusal(
    runtime: ConnectionObservation | None,
    migration: ConnectionObservation | None,
) -> str | None:
    """Use the checked-in role contract and exact observed backend identity."""

    if runtime is None or migration is None:
        return "incomplete_observation"
    principal = runtime.principal
    expected = COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT["app_user"]
    runtime_flags = (
        principal.can_login,
        principal.bypass_rls,
        principal.superuser,
        principal.can_create_database,
        principal.can_create_role,
    )
    if (
        principal.session_user != "app_user"
        or principal.current_user != "app_user"
        or runtime_flags != expected.authority_posture
        or principal.database_create
    ):
        return "runtime_principal"
    if not migration_principal_is_valid(migration.principal):
        return "migration_principal"
    if runtime.backend != migration.backend:
        return "backend_mismatch"
    return None


def _psycopg_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def observe_connection(url: str) -> ConnectionObservation | None:
    """Use one bounded read-only catalog transaction under the actual login."""

    with psycopg.connect(
        _psycopg_url(url),
        connect_timeout=10,
        autocommit=False,
    ) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL statement_timeout = 10000")
        row = connection.execute(_CATALOG_SQL).fetchone()
        return decode_observation(row)


def main() -> int:
    runtime_url = os.environ.get("DATABASE_PAIR_RUNTIME_URL")
    migration_url = os.environ.get("MIGRATION_DATABASE_URL")
    if not runtime_url or not migration_url or runtime_url == migration_url:
        print("database pair refused: missing_or_shared_url", file=sys.stderr)
        return 2
    try:
        runtime = observe_connection(runtime_url)
        migration = observe_connection(migration_url)
    except Exception:  # noqa: BLE001 - never expose connection/DSN diagnostics
        print("database pair refused: connection_or_catalog_error", file=sys.stderr)
        return 2
    reason = pair_refusal(runtime, migration)
    if reason is not None:
        print(f"database pair refused: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
