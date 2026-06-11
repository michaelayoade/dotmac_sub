"""Tests for the suspension-enforcement reconciliation audit.

The audit asserts "every fully-blocked subscriber is actually unreachable"
against the external RADIUS DB (sqlite stand-in here, same pattern as
test_radius_set_access_state.py).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.models.catalog import AccessCredential, Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber
from app.services.radius_reconciliation import audit_suspension_enforcement


def _ts(dt: datetime) -> str:
    """Match SQLAlchemy's sqlite datetime bind format so lexicographic
    comparison in the WHERE clause behaves like a real timestamp compare."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f") + "+00:00"


def _seed_radius_sqlite(
    db_path,
    *,
    radcheck=(),
    radusergroup=(),
    radacct=(),
):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE radcheck (username TEXT, attribute TEXT, value TEXT)"
        )
        conn.execute("CREATE TABLE radusergroup (username TEXT, groupname TEXT)")
        conn.execute(
            "CREATE TABLE radacct (username TEXT, acctstoptime TIMESTAMP, "
            "acctupdatetime TIMESTAMP)"
        )
        conn.executemany("INSERT INTO radcheck VALUES (?, ?, ?)", radcheck)
        conn.executemany("INSERT INTO radusergroup VALUES (?, ?)", radusergroup)
        conn.executemany("INSERT INTO radacct VALUES (?, ?, ?)", radacct)
        conn.commit()
    finally:
        conn.close()


def _fake_config(db_path):
    return {
        "db_url": f"sqlite:///{db_path}",
        "radcheck_table": "radcheck",
        "radreply_table": "radreply",
        "radusergroup_table": "radusergroup",
        "nas_table": "nas",
    }


def _seed_subscriber(db_session, *, username, statuses, offer):
    subscriber = Subscriber(
        first_name="Audit",
        last_name="Case",
        email=f"{username}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    for status in statuses:
        db_session.add(
            Subscription(
                subscriber_id=subscriber.id,
                offer_id=offer.id,
                status=status,
            )
        )
    db_session.add(
        AccessCredential(
            subscriber_id=subscriber.id,
            username=username,
            is_active=True,
        )
    )
    db_session.commit()
    return subscriber


def _run_audit(db_session, db_path):
    with patch(
        "app.services.radius_reconciliation._active_external_sync_configs",
        return_value=[_fake_config(db_path)],
    ):
        return audit_suspension_enforcement(db_session)


class TestAuditSuspensionEnforcement:
    def test_clean_when_nothing_blocked(self, db_session, tmp_path, catalog_offer):
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(db_path)
        _seed_subscriber(
            db_session,
            username="active-user",
            statuses=[SubscriptionStatus.active],
            offer=catalog_offer,
        )
        result = _run_audit(db_session, db_path)
        assert result["ok"] is True
        assert result["checked_usernames"] == 0

    def test_detects_all_leak_classes(self, db_session, tmp_path, catalog_offer):
        now = datetime.now(UTC)
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[
                ("blocked-user", "Cleartext-Password", "pw"),
                # Control: active subscriber's rows must not be flagged.
                ("active-user", "Cleartext-Password", "pw"),
            ],
            radusergroup=[
                ("blocked-user", "dotmac-active"),
                ("active-user", "dotmac-active"),
            ],
            radacct=[
                ("blocked-user", None, _ts(now - timedelta(minutes=10))),
                ("active-user", None, _ts(now - timedelta(minutes=10))),
            ],
        )
        _seed_subscriber(
            db_session,
            username="blocked-user",
            statuses=[SubscriptionStatus.suspended],
            offer=catalog_offer,
        )
        _seed_subscriber(
            db_session,
            username="active-user",
            statuses=[SubscriptionStatus.active],
            offer=catalog_offer,
        )

        result = _run_audit(db_session, db_path)

        assert result["checked_usernames"] == 1
        assert result["counts"] == {
            "usable_password": 1,
            "in_active_group": 1,
            "open_session": 1,
        }
        assert result["usable_password"] == ["blocked-user"]
        assert result["ok"] is False

    def test_reject_row_clears_password_leak(
        self, db_session, tmp_path, catalog_offer
    ):
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[
                ("blocked-user", "Cleartext-Password", "pw"),
                ("blocked-user", "Auth-Type", "Reject"),
            ],
        )
        _seed_subscriber(
            db_session,
            username="blocked-user",
            statuses=[SubscriptionStatus.suspended],
            offer=catalog_offer,
        )
        result = _run_audit(db_session, db_path)
        assert result["counts"]["usable_password"] == 0
        assert result["ok"] is True

    def test_stale_open_session_not_counted(
        self, db_session, tmp_path, catalog_offer
    ):
        now = datetime.now(UTC)
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radacct=[("blocked-user", None, _ts(now - timedelta(hours=5)))],
        )
        _seed_subscriber(
            db_session,
            username="blocked-user",
            statuses=[SubscriptionStatus.suspended],
            offer=catalog_offer,
        )
        result = _run_audit(db_session, db_path)
        assert result["counts"]["open_session"] == 0

    def test_mixed_status_subscriber_excluded_but_counted(
        self, db_session, tmp_path, catalog_offer
    ):
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("mixed-user", "Cleartext-Password", "pw")],
        )
        _seed_subscriber(
            db_session,
            username="mixed-user",
            statuses=[SubscriptionStatus.suspended, SubscriptionStatus.active],
            offer=catalog_offer,
        )
        result = _run_audit(db_session, db_path)
        # The shared credential keeps access by design (most-permissive
        # aggregate) — not a leak, but surfaced for visibility.
        assert result["checked_usernames"] == 0
        assert result["mixed_status_subscribers"] == 1
        assert result["ok"] is True

    def test_no_external_config_reports_error(
        self, db_session, tmp_path, catalog_offer
    ):
        _seed_subscriber(
            db_session,
            username="blocked-user",
            statuses=[SubscriptionStatus.suspended],
            offer=catalog_offer,
        )
        with patch(
            "app.services.radius_reconciliation._active_external_sync_configs",
            return_value=[],
        ):
            result = audit_suspension_enforcement(db_session)
        assert result["ok"] is False
        assert result["errors"] == 1
