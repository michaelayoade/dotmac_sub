"""Operational and explicit historical relay bootstrap on disposable PostgreSQL."""

from __future__ import annotations

import psycopg
import pytest
from sqlalchemy.engine import URL

from scripts.bootstrap_commercial_module_prereqs import bootstrap as bootstrap_modules
from scripts.bootstrap_outbox_dispatcher_roles import (
    bootstrap as bootstrap_outbox,
)
from scripts.bootstrap_outbox_dispatcher_roles import (
    verify as verify_outbox,
)
from scripts.ci.bootstrap_test_database_prereqs import _prepare_historical_557_replay
from tests.integration import test_kernel_lineage_rehearsal as kernel_rehearsal

isolated_database = kernel_rehearsal.isolated_database
_psycopg_url = kernel_rehearsal._psycopg_url


def test_default_outbox_repair_keeps_retired_membership_absent(
    isolated_database: URL,
) -> None:
    admin = psycopg.connect(_psycopg_url(isolated_database), autocommit=False)
    try:
        assert bootstrap_modules(admin, dry_run=False, repair=True) == 0
        admin.execute("REVOKE app_admin FROM dotmac_app")
        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 0
        assert not admin.execute(
            "SELECT pg_has_role('dotmac_app', 'app_admin', 'MEMBER')"
        ).fetchone()[0]
        assert verify_outbox(admin) == 0
    finally:
        admin.rollback()
        admin.close()


def test_historical_557_replay_preparation_is_explicit_and_not_operational(
    isolated_database: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = psycopg.connect(_psycopg_url(isolated_database), autocommit=False)
    try:
        assert bootstrap_modules(admin, dry_run=False, repair=True) == 0
        admin.execute("REVOKE app_admin FROM dotmac_app")
        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 0
        monkeypatch.setenv("APP_ENV", "test")
        _prepare_historical_557_replay(admin, isolated_database)
        assert admin.execute(
            "SELECT pg_has_role('dotmac_app', 'app_admin', 'MEMBER')"
        ).fetchone()[0]
        assert verify_outbox(admin) == 1
        # Default repair fails before any role write and never retires this
        # historical link automatically; retirement has separate authority.
        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 1
    finally:
        admin.rollback()
        admin.close()


def test_outbox_bootstrap_repairs_public_schema_ownership_privileges(
    isolated_database: URL,
) -> None:
    admin = psycopg.connect(_psycopg_url(isolated_database), autocommit=False)
    try:
        assert bootstrap_modules(admin, dry_run=False, repair=True) == 0
        admin.execute("REVOKE app_admin FROM dotmac_app")
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
        assert not admin.execute(
            "SELECT pg_has_role('dotmac_app', 'app_admin', 'MEMBER')"
        ).fetchone()[0]
        assert verify_outbox(admin) == 0
    finally:
        admin.rollback()
        admin.close()
