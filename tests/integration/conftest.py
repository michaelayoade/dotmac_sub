"""Integration flow tests: whole-module journeys on real PostgreSQL.

These reuse the root conftest's ``engine``/``db_session`` fixtures. The
authoritative integration target is explicit PostgreSQL/PostGIS, created by
the real Alembic chain through ``make test-integration``. SQLite and
``Base.metadata.create_all`` are rejected rather than silently skipped: flow
tests exist to exercise the deployed PG-only surface (JSONB operators, FK
cascades, row locks, migration-owned constraints, indexes and triggers) the
fast unit lane does not claim to represent.

Each test drives a migrated module's NATIVE path end-to-end with its Phase 3
flag ON via ``enable_flags`` — the flag-off write-throughs stay covered by
the unit suite.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace

import pytest
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.services import control_registry
from scripts.ci import template_database


@pytest.fixture(autouse=True)
def _require_postgres(engine):
    if engine.dialect.name != "postgresql":
        raise pytest.UsageError(
            "integration flows require migrated PostgreSQL; run "
            "`make test-integration` with TEST_DATABASE_URL pointing at a "
            "disposable test database"
        )


@pytest.fixture
def enable_flags(db_session: Session):
    """Flip Phase 3 controls for one test (rows roll back with the session).

    Controls resolve exclusively from their canonical ``modules.<feature>``
    row (``control_registry._resolve_own_flag`` — retired legacy aliases are
    deliberately ignored), so that is what we write. Accepts either the
    control key ("quotes.native_write") or its legacy setting name
    ("quotes_native_write_enabled") for readability at call sites.
    """

    def _enable(*keys: str) -> None:
        for key in keys:
            control = control_registry._CONTROLS.get(key)
            if control is None:
                # legacy-name convenience: find the control by alias
                dotted = key.removesuffix("_enabled").replace("_native_", ".native_")
                control = control_registry._CONTROLS.get(dotted)
            assert control is not None, f"unknown control for {key!r}"
            db_session.add(
                DomainSetting(
                    domain=SettingDomain.modules,
                    key=control_registry.canonical_setting_key(control),
                    value_type=SettingValueType.boolean,
                    value_text="true",
                    is_active=True,
                )
            )
        db_session.flush()

    return _enable


# --------------------------------------------------------------------------
# Migrated template databases
# --------------------------------------------------------------------------
#
# A test needing a database OF ITS OWN used to create an empty one and replay
# the whole Alembic chain -- about 50 s against 601 revisions, per test. The
# approved standard (see `scripts/ci/template_database.py`) is to run the real
# chain ONCE per revision target into a sealed template, then hand each test a
# byte-identical copy.
#
# Use `cloned_database` for a test whose subject is BEHAVIOUR on a migrated
# schema. A test whose subject is the act of migrating keeps replaying the
# chain: cloning would move the thing under test into the fixture.


def _integration_base_url() -> URL:
    """Resolve the integration target, or fail LOUDLY.

    Deliberately a refusal rather than a skipped test. Skipping would let the
    PostgreSQL lane report green having executed nothing -- the exact silent
    degradation this package exists to prevent, and what
    `test_migrated_database_test_contract.py` forbids by inspecting this file's
    source. Same refusal shape as `_require_postgres` above.
    """

    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        raise pytest.UsageError(
            "template-database fixtures require TEST_DATABASE_URL pointing at a "
            "disposable migrated PostgreSQL database; run `make test-integration`"
        )
    url = make_url(configured)
    if not url.drivername.startswith("postgresql"):
        raise pytest.UsageError(
            "template-database fixtures require PostgreSQL/PostGIS; SQLite and "
            "metadata-built schemas are not deployed-schema evidence"
        )
    return url


@pytest.fixture(scope="session")
def template_base_url() -> URL:
    """The server the templates and clones live on.

    Deliberately performs no startup sweep. A wildcard cleanup can only match a
    name prefix, and a prefix cannot distinguish a crashed run's residue from a
    CONCURRENT run's live databases -- so an automatic sweep risks destroying
    another shard's work to reclaim disk that ephemeral CI reclaims anyway.
    Teardown below removes this run's own databases by name; anything older is
    the explicit `python -m scripts.ci.template_database` maintenance command's
    business.
    """

    return _integration_base_url()


@pytest.fixture(scope="session")
def migrated_template(
    template_base_url: URL,
) -> Iterator[Callable[[str], URL]]:
    """Build at most one sealed template per revision target, for the session."""

    built: dict[str, URL] = {}
    # `revision not in built` followed by `create_template` is check-then-act:
    # two concurrent requests for one target would both miss and both replay the
    # chain, which is the exact cost this fixture exists to avoid paying twice.
    lock = threading.Lock()

    def _for(revision: str) -> URL:
        with lock:
            if revision not in built:
                built[revision] = template_database.create_template(
                    template_base_url, revision
                )
            return built[revision]

    try:
        yield _for
    finally:
        for template in built.values():
            assert template.database
            template_database.drop_database(template_base_url, template.database)


@pytest.fixture
def cloned_database(
    template_base_url: URL,
    migrated_template: Callable[[str], URL],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[str], URL]]:
    """A disposable copy of the migrated schema at a named revision.

    The copy is writable and private to one test, so a test may mutate it
    freely -- including downgrading it -- without touching the sealed template
    every other test clones from.
    """

    clones: list[URL] = []

    def _at(revision: str) -> URL:
        template = migrated_template(revision)
        clone = template_database.clone_from_template(template_base_url, template)
        clones.append(clone)
        # `alembic/env.py` resolves its target from `app_config.settings`, so a
        # test that runs Alembic against its clone needs this, not the Config's
        # `sqlalchemy.url`.
        from app import config as app_config

        monkeypatch.setattr(
            app_config,
            "settings",
            replace(
                app_config.settings,
                database_url=clone.render_as_string(hide_password=False),
            ),
        )
        return clone

    try:
        yield _at
    finally:
        # Runs on the exception path too, so a failing test drops its own clone
        # and only its own -- the list holds exactly what this test created.
        for clone in clones:
            assert clone.database
            template_database.drop_database(template_base_url, clone.database)
