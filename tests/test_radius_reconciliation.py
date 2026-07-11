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
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.radius_reconciliation import audit_suspension_enforcement


def _ts(dt: datetime) -> str:
    """Match SQLAlchemy's sqlite datetime bind format so lexicographic
    comparison in the WHERE clause behaves like a real timestamp compare."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f") + "+00:00"


def _seed_radius_sqlite(
    db_path,
    *,
    radcheck=(),
    radreply=(),
    radusergroup=(),
    radacct=(),
):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE radcheck (username TEXT, attribute TEXT, value TEXT)"
        )
        conn.execute(
            "CREATE TABLE radreply (username TEXT, attribute TEXT, value TEXT)"
        )
        conn.execute("CREATE TABLE radusergroup (username TEXT, groupname TEXT)")
        conn.execute(
            "CREATE TABLE radacct (username TEXT, acctstoptime TIMESTAMP, "
            "acctupdatetime TIMESTAMP)"
        )
        conn.executemany("INSERT INTO radcheck VALUES (?, ?, ?)", radcheck)
        conn.executemany("INSERT INTO radreply VALUES (?, ?, ?)", radreply)
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


def _seed_subscriber(
    db_session, *, username, statuses, offer, account_status=SubscriberStatus.active
):
    subscriber = Subscriber(
        first_name="Audit",
        last_name="Case",
        email=f"{username}@example.com",
        status=account_status,
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
            "open_access": 1,
            "in_active_group": 1,
            "open_session": 1,
        }
        assert result["open_access"] == ["blocked-user"]
        assert result["ok"] is False

    def test_reject_row_clears_password_leak(self, db_session, tmp_path, catalog_offer):
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
        assert result["counts"]["open_access"] == 0
        assert result["ok"] is True

    def test_parent_blocked_active_subscription_is_audited(
        self, db_session, tmp_path, catalog_offer
    ):
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[("disabled-active-user", "Cleartext-Password", "pw")],
            radusergroup=[("disabled-active-user", "dotmac-active")],
            radacct=[
                (
                    "disabled-active-user",
                    None,
                    _ts(datetime.now(UTC) - timedelta(minutes=10)),
                )
            ],
        )
        _seed_subscriber(
            db_session,
            username="disabled-active-user",
            statuses=[SubscriptionStatus.active],
            offer=catalog_offer,
            account_status=SubscriberStatus.disabled,
        )

        result = _run_audit(db_session, db_path)

        assert result["checked_usernames"] == 1
        assert result["counts"] == {
            "open_access": 1,
            "in_active_group": 1,
            "open_session": 1,
        }
        assert result["ok"] is False

    def test_walled_garden_marker_is_by_design(
        self, db_session, tmp_path, catalog_offer
    ):
        """Captive-by-default: a blocked subscriber with the walled-garden
        address-list radreply (or captive group) keeps a usable password ON
        PURPOSE — they can reach the pay page. Their open session is equally
        by design."""
        now = datetime.now(UTC)
        db_path = tmp_path / "radius.db"
        _seed_radius_sqlite(
            db_path,
            radcheck=[
                ("walled-user", "Cleartext-Password", "pw"),
                ("captive-group-user", "Cleartext-Password", "pw"),
            ],
            radreply=[("walled-user", "Mikrotik-Address-List", "suspended")],
            radusergroup=[("captive-group-user", "dotmac-captive")],
            radacct=[("walled-user", None, _ts(now - timedelta(minutes=5)))],
        )
        for username in ("walled-user", "captive-group-user"):
            _seed_subscriber(
                db_session,
                username=username,
                statuses=[SubscriptionStatus.suspended],
                offer=catalog_offer,
            )
        result = _run_audit(db_session, db_path)
        assert result["checked_usernames"] == 2
        assert result["counts"] == {
            "open_access": 0,
            "in_active_group": 0,
            "open_session": 0,
        }
        assert result["ok"] is True

    def test_stale_open_session_not_counted(self, db_session, tmp_path, catalog_offer):
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


class TestAuditResultStorageAndCollector:
    def _fake_redis(self):
        class FakeRedis:
            def __init__(self):
                self.store = {}

            def set(self, key, value, ex=None):
                self.store[key] = value

            def get(self, key):
                return self.store.get(key)

        return FakeRedis()

    def test_store_and_load_roundtrip(self):
        from app.services import radius_reconciliation as rr

        fake = self._fake_redis()
        with patch.object(rr, "_get_redis", return_value=fake):
            assert rr.store_latest_audit(
                {"counts": {"open_access": 2}, "mixed_status_subscribers": 5}
            )
            loaded = rr.load_latest_audit()
        assert loaded["counts"] == {"open_access": 2}
        assert loaded["mixed_status_subscribers"] == 5
        assert "ran_at" in loaded

    def test_store_without_redis_is_failsoft(self):
        from app.services import radius_reconciliation as rr

        with patch.object(rr, "_get_redis", return_value=None):
            assert rr.store_latest_audit({"counts": {}}) is False
            assert rr.load_latest_audit() is None

    def test_collector_exports_counts_and_age(self):
        """The audit runs in a worker; the web process's collector must
        export the stored result as gauges at scrape time."""
        from datetime import UTC, datetime

        from app.metrics import _SuspensionAuditCollector

        data = {
            "counts": {"open_access": 3, "in_active_group": 0, "open_session": 1},
            "mixed_status_subscribers": 7,
            "ran_at": datetime.now(UTC).isoformat(),
        }
        with patch(
            "app.services.radius_reconciliation.load_latest_audit",
            return_value=data,
        ):
            families = list(_SuspensionAuditCollector().collect())

        by_name = {f.name: f for f in families}
        leaks = by_name["radius_suspension_audit_leaks"]
        samples = {s.labels["kind"]: s.value for s in leaks.samples}
        assert samples["open_access"] == 3
        assert samples["open_session"] == 1
        assert samples["mixed_status_subscribers"] == 7
        age = by_name["radius_suspension_audit_age_seconds"]
        assert age.samples[0].value >= 0

    def test_collector_silent_without_data(self):
        from app.metrics import _SuspensionAuditCollector

        with patch(
            "app.services.radius_reconciliation.load_latest_audit",
            return_value=None,
        ):
            assert list(_SuspensionAuditCollector().collect()) == []
