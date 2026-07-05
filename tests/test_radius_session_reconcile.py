"""Tests for the radacct -> radius_active_sessions live-view reconciler.

The event-driven populator (accounting hooks) isn't firing in prod, so this
sweep rediscovers OPEN sessions from the external FreeRADIUS ``radacct`` table
(sqlite stand-in here, same pattern as test_radius_reconciliation.py) and
upserts them into ``radius_active_sessions``, pruning ended ones.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.services.radius_session_reconcile import (
    reconcile_active_sessions_from_radacct,
)


def _dt(dt: datetime) -> str:
    """Naive UTC string SQLAlchemy's sqlite DateTime can round-trip."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _seed_radacct_sqlite(db_path, *, rows=()):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE radacct ("
            "username TEXT, acctsessionid TEXT, callingstationid TEXT, "
            "framedipaddress TEXT, framedipv6prefix TEXT, nasipaddress TEXT, "
            "nasportid TEXT, acctstarttime TIMESTAMP, acctstoptime TIMESTAMP, "
            "acctupdatetime TIMESTAMP)"
        )
        conn.executemany(
            "INSERT INTO radacct VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        conn.commit()
    finally:
        conn.close()


def _fake_config(db_path):
    return {"db_url": f"sqlite:///{db_path}"}


def _open_row(
    *,
    username,
    sid,
    calling="AA:BB:CC:DD:EE:FF",
    framed="100.64.0.1",
    framed6=None,
    nasip="10.0.0.1",
    nasport="pppoe0",
    start=None,
    updated=None,
):
    now = datetime.now(UTC)
    return (
        username,
        sid,
        calling,
        framed,
        framed6,
        nasip,
        nasport,
        _dt(start or now - timedelta(minutes=30)),
        None,  # acctstoptime -> OPEN
        _dt(updated or now - timedelta(minutes=2)),
    )


def _seed_subscriber_with_login(db_session, *, login, offer, status, email=None):
    subscriber = Subscriber(
        first_name="Live",
        last_name="Session",
        email=email or f"{login}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        login=login,
    )
    db_session.add(sub)
    db_session.commit()
    return subscriber, sub


def _seed_nas(db_session, *, name, nas_ip):
    nas = NasDevice(name=name, nas_ip=nas_ip, is_active=True)
    db_session.add(nas)
    db_session.commit()
    return nas


def _run(db_session, db_path):
    with patch(
        "app.services.radius_session_reconcile._active_external_sync_configs",
        return_value=[_fake_config(db_path)],
    ):
        return reconcile_active_sessions_from_radacct(db_session)


class TestActiveSessionReconcile:
    def test_open_session_upserts_with_mappings(
        self, db_session, tmp_path, catalog_offer
    ):
        db_path = tmp_path / "radacct.db"
        subscriber, sub = _seed_subscriber_with_login(
            db_session,
            login="100017271",
            offer=catalog_offer,
            status=SubscriptionStatus.active,
        )
        nas = _seed_nas(db_session, name="bng-1", nas_ip="10.0.0.1")
        _seed_radacct_sqlite(
            db_path,
            rows=[_open_row(username="100017271", sid="sess-1", nasip="10.0.0.1")],
        )

        result = _run(db_session, db_path)

        assert result["seen_open"] == 1
        assert result["upserted_new"] == 1
        assert result["unmatched_username"] == 0
        assert result["unresolved_nas"] == 0

        row = db_session.query(RadiusActiveSession).one()
        assert row.acct_session_id == "sess-1"
        assert row.subscriber_id == subscriber.id
        assert row.subscription_id == sub.id
        assert row.nas_device_id == nas.id
        assert row.framed_ip_address == "100.64.0.1"
        assert row.calling_station_id == "AA:BB:CC:DD:EE:FF"

    def test_username_without_active_sub_is_unmatched_no_row(
        self, db_session, tmp_path, catalog_offer
    ):
        db_path = tmp_path / "radacct.db"
        # Subscriber exists but the sub is suspended, not active.
        _seed_subscriber_with_login(
            db_session,
            login="200000001",
            offer=catalog_offer,
            status=SubscriptionStatus.suspended,
        )
        _seed_radacct_sqlite(
            db_path, rows=[_open_row(username="200000001", sid="sess-x")]
        )

        result = _run(db_session, db_path)

        assert result["seen_open"] == 1
        assert result["unmatched_username"] == 1
        assert result["upserted_new"] == 0
        assert db_session.query(RadiusActiveSession).count() == 0

    def test_unresolved_nas_creates_row_with_null_nas(
        self, db_session, tmp_path, catalog_offer
    ):
        db_path = tmp_path / "radacct.db"
        _seed_subscriber_with_login(
            db_session,
            login="300000001",
            offer=catalog_offer,
            status=SubscriptionStatus.active,
        )
        # No NasDevice for this nasipaddress.
        _seed_radacct_sqlite(
            db_path,
            rows=[_open_row(username="300000001", sid="sess-n", nasip="10.9.9.9")],
        )

        result = _run(db_session, db_path)

        assert result["upserted_new"] == 1
        assert result["unresolved_nas"] == 1
        row = db_session.query(RadiusActiveSession).one()
        assert row.nas_device_id is None
        assert row.nas_ip_address == "10.9.9.9"

    def test_ended_session_pruned_on_next_run(
        self, db_session, tmp_path, catalog_offer
    ):
        db_path = tmp_path / "radacct.db"
        _seed_subscriber_with_login(
            db_session,
            login="400000001",
            offer=catalog_offer,
            status=SubscriptionStatus.active,
        )
        _seed_nas(db_session, name="bng-1", nas_ip="10.0.0.1")
        _seed_radacct_sqlite(
            db_path, rows=[_open_row(username="400000001", sid="sess-gone")]
        )
        first = _run(db_session, db_path)
        assert first["upserted_new"] == 1
        assert db_session.query(RadiusActiveSession).count() == 1

        # Session closed: radacct no longer lists it as open.
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM radacct")
        conn.commit()
        conn.close()

        second = _run(db_session, db_path)
        assert second["seen_open"] == 0
        assert second["pruned"] == 1
        assert db_session.query(RadiusActiveSession).count() == 0

    def test_idempotent_second_run(self, db_session, tmp_path, catalog_offer):
        db_path = tmp_path / "radacct.db"
        _seed_subscriber_with_login(
            db_session,
            login="500000001",
            offer=catalog_offer,
            status=SubscriptionStatus.active,
        )
        _seed_nas(db_session, name="bng-1", nas_ip="10.0.0.1")
        _seed_radacct_sqlite(
            db_path, rows=[_open_row(username="500000001", sid="sess-idem")]
        )
        first = _run(db_session, db_path)
        assert first["upserted_new"] == 1

        second = _run(db_session, db_path)
        assert second["seen_open"] == 1
        assert second["upserted_new"] == 0
        assert second["upserted_updated"] == 1
        assert second["pruned"] == 0
        assert db_session.query(RadiusActiveSession).count() == 1

    def test_duplicate_login_dedupe_is_deterministic(
        self, db_session, tmp_path, catalog_offer
    ):
        db_path = tmp_path / "radacct.db"
        # Two ACTIVE subscriptions share the same login (migration duplicates).
        subscriber, sub_a = _seed_subscriber_with_login(
            db_session,
            login="600000001",
            offer=catalog_offer,
            status=SubscriptionStatus.active,
        )
        sub_b = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
            login="600000001",
        )
        db_session.add(sub_b)
        db_session.commit()
        _seed_nas(db_session, name="bng-1", nas_ip="10.0.0.1")
        _seed_radacct_sqlite(
            db_path, rows=[_open_row(username="600000001", sid="sess-dup")]
        )

        result = _run(db_session, db_path)
        assert result["upserted_new"] == 1
        row = db_session.query(RadiusActiveSession).one()
        # Deterministic: lowest subscription id wins.
        expected = min([sub_a.id, sub_b.id], key=lambda u: str(u))
        assert row.subscription_id == expected

    def test_advisory_lock_single_flight_skips(self):
        from app import tasks as _tasks  # noqa: F401  ensure task registered
        from app.tasks.radius import reconcile_active_sessions

        @contextmanager
        def _not_acquired(*args, **kwargs):
            yield (object(), False)

        with (
            patch("app.tasks.radius.db_session_adapter.advisory_lock", _not_acquired),
            patch(
                "app.services.radius_session_reconcile."
                "reconcile_active_sessions_from_radacct"
            ) as spy,
        ):
            out = reconcile_active_sessions.run()
        assert out == {"skipped": "already_running"}
        spy.assert_not_called()
