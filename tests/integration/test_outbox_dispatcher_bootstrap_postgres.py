"""Production-shaped bootstrap coverage for outbox relay prerequisites."""

from __future__ import annotations

import psycopg
from sqlalchemy.engine import URL

from scripts.bootstrap_commercial_module_prereqs import bootstrap as bootstrap_modules
from scripts.bootstrap_outbox_dispatcher_roles import (
    bootstrap as bootstrap_outbox,
)
from scripts.bootstrap_outbox_dispatcher_roles import (
    verify as verify_outbox,
)
from tests.integration import test_kernel_lineage_rehearsal as kernel_rehearsal

isolated_database = kernel_rehearsal.isolated_database
_psycopg_url = kernel_rehearsal._psycopg_url


def test_outbox_bootstrap_repairs_migration_role_membership(
    isolated_database: URL,
) -> None:
    with psycopg.connect(_psycopg_url(isolated_database), autocommit=False) as admin:
        assert bootstrap_modules(admin, dry_run=False, repair=True) == 0
        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 0
        admin.execute("REVOKE app_admin FROM dotmac_app")
        assert verify_outbox(admin) == 1

        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 0

        assert admin.execute(
            "SELECT pg_has_role('dotmac_app', 'app_admin', 'MEMBER')"
        ).fetchone()[0]
        assert verify_outbox(admin) == 0


def test_outbox_bootstrap_repairs_public_schema_ownership_privileges(
    isolated_database: URL,
) -> None:
    with psycopg.connect(_psycopg_url(isolated_database), autocommit=False) as admin:
        assert bootstrap_modules(admin, dry_run=False, repair=True) == 0
        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 0
        admin.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC, app_admin")
        assert verify_outbox(admin) == 1

        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 0

        assert admin.execute(
            "SELECT has_schema_privilege('app_admin', 'public', 'USAGE')"
        ).fetchone()[0]
        assert admin.execute(
            "SELECT has_schema_privilege('app_admin', 'public', 'CREATE')"
        ).fetchone()[0]
        assert verify_outbox(admin) == 0
