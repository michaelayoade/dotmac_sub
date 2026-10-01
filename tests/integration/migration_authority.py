"""Explicit migration login for one disposable PostgreSQL rehearsal database."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.engine import URL, make_url

from scripts.ci.bootstrap_test_database_prereqs import bootstrap_disposable_database


@contextmanager
def migration_database(target: URL) -> Iterator[None]:
    """Provision a clone, then point Alembic at its actual app_admin login.

    The caller passes the exact isolated database it just created. There is no
    inference from DATABASE_URL or Alembic's config, and a rehearsal cannot
    accidentally migrate the shared integration target.
    """

    configured = os.environ.get("TEST_DATABASE_URL")
    if not configured:
        raise RuntimeError("TEST_DATABASE_URL is required for migration rehearsal")
    base = make_url(configured)
    if (
        target.drivername != base.drivername
        or target.host != base.host
        or target.port != base.port
        or target.database == base.database
        or not (target.database or "").startswith("dotmac_")
    ):
        raise RuntimeError(
            "migration rehearsal target is not an isolated test database"
        )

    if bootstrap_disposable_database(target, label=target.database or "isolated"):
        raise RuntimeError("disposable migration prerequisites are not satisfied")

    previous = os.environ.get("MIGRATION_DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = target.set(
        username="app_admin"
    ).render_as_string(hide_password=False)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
        else:
            os.environ["MIGRATION_DATABASE_URL"] = previous
