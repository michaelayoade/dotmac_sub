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


def _edges(admin: psycopg.Connection) -> tuple[bool, bool]:
    new = admin.execute(
        "SELECT pg_has_role('app_admin', 'dotmac_app', 'USAGE') "
        "AND pg_has_role('app_admin', 'dotmac_app', 'SET')"
    ).fetchone()[0]
    old = admin.execute(
        "SELECT pg_has_role('dotmac_app', 'app_admin', 'MEMBER')"
    ).fetchone()[0]
    return bool(new), bool(old)


def test_outbox_bootstrap_creates_the_migrator_owner_edge(
    isolated_database: URL,
) -> None:
    with psycopg.connect(_psycopg_url(isolated_database), autocommit=False) as admin:
        assert bootstrap_modules(admin, dry_run=False, repair=True) == 0
        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 0

        assert _edges(admin) == (True, False)
        assert not admin.execute(
            "SELECT admin_option FROM pg_auth_members "
            "WHERE roleid = 'dotmac_app'::regrole AND member = 'app_admin'::regrole"
        ).fetchone()[0]
        assert verify_outbox(admin) == 0


def test_outbox_bootstrap_repairs_a_missing_migrator_membership(
    isolated_database: URL,
) -> None:
    with psycopg.connect(_psycopg_url(isolated_database), autocommit=False) as admin:
        assert bootstrap_modules(admin, dry_run=False, repair=True) == 0
        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 0
        admin.execute("REVOKE dotmac_app FROM app_admin")
        assert verify_outbox(admin) == 1

        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 0

        assert _edges(admin) == (True, False)
        assert verify_outbox(admin) == 0


def test_outbox_bootstrap_removes_the_old_direction_and_never_regrants_it(
    isolated_database: URL,
) -> None:
    with psycopg.connect(_psycopg_url(isolated_database), autocommit=False) as admin:
        assert bootstrap_modules(admin, dry_run=False, repair=True) == 0
        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 0
        admin.execute("GRANT app_admin TO dotmac_app")
        assert _edges(admin) == (True, True)
        assert verify_outbox(admin) == 1

        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 0

        assert _edges(admin) == (True, False)
        assert verify_outbox(admin) == 0
        # A second repair is a no-op and still does not restore the old edge.
        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 0
        assert _edges(admin) == (True, False)


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
