"""Production-shaped bootstrap coverage for commercial module prerequisites."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import URL

from scripts.bootstrap_commercial_module_prereqs import bootstrap, verify
from tests.integration import test_kernel_lineage_rehearsal as kernel_rehearsal

isolated_database = kernel_rehearsal.isolated_database
_psycopg_url = kernel_rehearsal._psycopg_url


def test_bootstrap_allows_restricted_verify_without_database_create(
    isolated_database: URL,
) -> None:
    database_name = isolated_database.database
    assert database_name is not None

    with psycopg.connect(_psycopg_url(isolated_database), autocommit=False) as admin:
        assert bootstrap(admin, dry_run=False, repair=True) == 0
        admin.execute(
            sql.SQL("REVOKE CREATE ON DATABASE {} FROM PUBLIC, dotmac_app").format(
                sql.Identifier(database_name)
            )
        )
        admin.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO dotmac_app").format(
                sql.Identifier(database_name)
            )
        )
        assert bootstrap(admin, dry_run=False, repair=True) == 0

    with psycopg.connect(_psycopg_url(isolated_database), autocommit=True) as conn:
        conn.execute("SET ROLE dotmac_app")
        assert not conn.execute(
            "SELECT has_database_privilege('dotmac_app', current_database(), 'CREATE')"
        ).fetchone()[0]

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE SCHEMA mod_restricted_probe")

        assert verify(conn) == 0
