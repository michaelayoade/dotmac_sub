"""Enforcement paths must resolve the radius DSN via radius_dsn (the owner).

``radius_dsn.resolve_radius_dsn()`` defines the precedence
RADIUS_SYNC_DB_URL → RADIUS_DB_DSN → RADIUS_DB_* parts. A site that reads
``os.environ["RADIUS_DB_DSN"]`` raw gets an empty string when prod is
configured with either other form — and these particular sites then fail
OPEN: no live sessions found → suspend/disable sends no CoA, and the
enforcement reconciler reports all-zeros (the silent-miss class of
incident 2026-06-11). Each test sets ONLY RADIUS_SYNC_DB_URL and asserts
the site still reaches the radius DB.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

_DSN = "postgresql://u:p@radius-test:5432/radius"


@pytest.fixture()
def _sync_url_only(monkeypatch):
    monkeypatch.setenv("RADIUS_SYNC_DB_URL", _DSN)
    monkeypatch.setenv("RADIUS_DB_DSN", "")


def _fake_conn(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


def test_open_radacct_sessions_resolves_dsn_canonically(_sync_url_only):
    from app.services.enforcement import _open_radacct_sessions_for_username

    with patch("psycopg.connect", return_value=_fake_conn([])) as mock_connect:
        _open_radacct_sessions_for_username("100088888")

    mock_connect.assert_called_once_with(_DSN)


def test_nas_secret_lookup_resolves_dsn_canonically(_sync_url_only):
    from app.services.enforcement import _nas_secret_from_radius_db

    with patch(
        "psycopg.connect", return_value=_fake_conn([("s3cret",)])
    ) as mock_connect:
        secret = _nas_secret_from_radius_db("10.0.0.1")

    mock_connect.assert_called_once_with(_DSN)
    assert secret == "s3cret"


def test_radius_accounting_db_url_resolves_dsn_canonically(_sync_url_only):
    from app.services.usage import _radius_accounting_db_url

    # SQLAlchemy form of the same target — the accounting importer must read
    # the SAME database the RADIUS writers write, whatever env form is set.
    assert (
        _radius_accounting_db_url()
        == "postgresql+psycopg://u:p@radius-test:5432/radius"
    )


def test_enforcement_reconciler_resolves_dsn_canonically(_sync_url_only, db_session):
    with (
        patch("app.db.SessionLocal", return_value=db_session),
        patch("psycopg.connect", return_value=_fake_conn([])) as mock_connect,
        patch("app.services.radius_reject.get_reject_networks", return_value={}),
    ):
        from app.tasks.radius import run_enforcement_reconciler

        stats = run_enforcement_reconciler()

    # The raw-env version bails out ("RADIUS_DB_DSN not set") without ever
    # touching the radius DB; the canonical resolver must reach it.
    mock_connect.assert_called_with(_DSN)
    assert stats["stale_unserviceable_sessions"] == 0
