"""Runtime RADIUS readers use DB configuration, never the legacy env DSN."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from unittest.mock import MagicMock, patch


def _target(url: str):
    return {
        "db_url": url,
        "radacct_table": "radacct",
        "radcheck_table": "radcheck",
        "radreply_table": "radreply",
        "radusergroup_table": "radusergroup",
        "nas_table": "nas",
        "target_fingerprint": "test-radius",
        "use_group": False,
    }


def test_open_sessions_reads_configured_accounting_target(
    db_session, tmp_path, monkeypatch
):
    path = tmp_path / "accounting.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE radacct (username TEXT, acctsessionid TEXT, "
            "nasipaddress TEXT, framedipaddress TEXT, acctstoptime TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO radacct VALUES (?, ?, ?, ?, NULL)",
            ("customer", "session-1", "10.0.0.1", "100.64.0.1"),
        )
    monkeypatch.setenv(
        "RADIUS_SYNC_DB_URL", "postgresql://wrong:wrong@wrong.invalid/wrong"
    )
    monkeypatch.setattr(
        "app.services.external_radius_targets.authoritative_accounting_target",
        lambda _db: _target(f"sqlite:///{path}"),
    )
    from app.services.enforcement import _open_radacct_sessions_for_username

    assert _open_radacct_sessions_for_username(db_session, "customer") == [
        {
            "session_id": "session-1",
            "nas_ip": "10.0.0.1",
            "framed_ip": "100.64.0.1",
        }
    ]


def test_usage_resolves_db_configured_accounting_target(db_session, monkeypatch):
    configured = "postgresql+psycopg://configured.example/radius"
    monkeypatch.setenv(
        "RADIUS_SYNC_DB_URL", "postgresql://wrong:wrong@wrong.invalid/wrong"
    )
    monkeypatch.setattr(
        "app.services.external_radius_targets.authoritative_accounting_target",
        lambda _db: _target(configured),
    )
    from app.services.usage import _radius_accounting_db_url

    assert _radius_accounting_db_url(db_session) == configured


def _fake_conn():
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    return connection


def test_enforcement_reconciler_reads_db_configured_target(db_session, monkeypatch):
    configured = "postgresql+psycopg://u:p@configured:5432/radius"
    target = {
        **_target(configured),
        "radacct_table": "radius.acct",
        "radcheck_table": "radius.checks",
        "radreply_table": "radius.replies",
    }
    connection = _fake_conn()
    cursor = connection.cursor.return_value.__enter__.return_value
    monkeypatch.setenv(
        "RADIUS_SYNC_DB_URL", "postgresql://wrong:wrong@wrong.invalid/wrong"
    )
    with (
        patch("app.db.SessionLocal", return_value=db_session),
        patch(
            "app.tasks.radius.postgres_session_advisory_lock",
            return_value=nullcontext(True),
        ),
        patch(
            "app.services.external_radius_targets.authoritative_accounting_target",
            return_value=target,
        ),
        patch("psycopg.connect", return_value=connection) as mock_connect,
        patch("app.services.radius_reject.get_reject_networks", return_value={}),
        patch(
            "app.services.radius_population.populate",
            return_value={
                "unbuildable_logins": 0,
                "expected_projection_fingerprints": {"test-radius": {}},
            },
        ),
    ):
        from app.tasks.radius import run_enforcement_reconciler

        stats = run_enforcement_reconciler()

    mock_connect.assert_called_with("postgresql://u:p@configured:5432/radius")
    rendered = [repr(call.args[0]) for call in cursor.execute.call_args_list]
    assert any("Identifier('radius', 'acct')" in query for query in rendered)
    assert any("Identifier('radius', 'checks')" in query for query in rendered)
    assert stats["stale_unserviceable_sessions"] == 0
